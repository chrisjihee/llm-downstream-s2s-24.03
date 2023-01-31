import os
# Disable TF-TRT Warnings, we don't want to use tf2 for tensorboard.
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import random
import functools
import argparse

from dataclasses import dataclass
from datetime import date

import tqdm
import numpy as np
import torch
import torch.nn.functional as F
import pytorch_lightning as pl
import evaluate

from looseversion import LooseVersion
from torch import nn
from torch.utils.data import DataLoader

from torch.distributed.fsdp.wrap import (transformer_auto_wrap_policy)
from transformers.models.t5.modeling_t5 import T5Block

from transformers import (
    AutoConfig, BertModel,
    AutoTokenizer,
)
from datasets import (load_from_disk, load_dataset, DatasetDict)
from pytorch_lightning.strategies import DeepSpeedStrategy
from pytorch_lightning.callbacks import Callback, ModelCheckpoint, LearningRateMonitor
from pytorch_lightning.utilities import rank_zero

# we need pytorch 1.12+
from pytorch_lightning.strategies import DDPFullyShardedNativeStrategy
from torch.distributed.fsdp.fully_sharded_data_parallel import CPUOffload
#from torch.distributed.fsdp import (FullyShardedDataParallel as FSDP,
#        FullStateDictConfig, StateDictType, MixedPrecision)

from models import test_helper
from models.mlm_plmodule_wrapper import ETRIT5ConditionalGenModelLightningModule
from datamodules.nsmc_pldm import NSMCDataModule
from datamodules.klue_nli_pldm import KLUENLIDataModule, KLUEYNATDataModule
from datamodules.kornli_pldm import KorNLIDataModule
from datamodules.pawsx_pldm import paws_xDataModule
from datamodules.kortrain_test import korTrainTextDataModule

from collators import (generic, klue, pawsx)

import task_utils


def get_argparser():
    """ generate argument parser. """
    parser = argparse.ArgumentParser(description="Train T5-like model with pytorch+transformers.")
    parser.add_argument("-tokenizer", type=str, default="google/byt5-small",
                        help="set hf tokenizer name or model path.")
    parser.add_argument("-seed", type=int, default=123456,
                        help="set a seed for RNGs. if you assign value below 0(e.g. -1), "
                        "we will randomize seed with secrets.randbelow() function.")
    parser.add_argument("-batch_size", type=int, default=128,
                        help="train/valid data batch size")
    parser.add_argument("-model", type=str, default="",
                        help="model path or hf-model name. e.g. google/byt5-small")
    parser.add_argument("-gpus", type=int, default=4,
                        help="number of accelerators(e.g. GPUs) for training.")
    parser.add_argument("-float_precision", type=int, default=32,
                        help="set floating point precision. default value is 32, you can set 16. with value 16, if bf16 supported, bf16 will be enabled automatically.")
    parser.add_argument("-task", type=str, default="nsmc-prompted",
                        help="set a downstream task. (nsmc-naive|nsmc-prompted|klue-nli-prompted|translate-ko-en)")
    return parser


if __name__ == '__main__':
    parser = get_argparser()
    args = parser.parse_args()

    if args.model == "":
        raise Exception("assign -model to inference a model. "
                        "e.g. -model google/byt5-small")

    if args.seed < 0:
        # python 3.6 or more needed to use secrets
        import secrets
        args.seed = secrets.randbelow(1_000_000_000)

    # global seed 초기화
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    pl.seed_everything(args.seed)

    # 기본적으로 Fused AdamW (DeepSpeed)는 Off, 32bit로 학습
    use_cpu_adam_arg = False
    precision_arg = args.float_precision
    callbacks = []

    if precision_arg != 32 and precision_arg != 16:
        raise Exception("bad argument: you can assign 32 or 16 for -float_precision")

    bf16_ready = (torch.version.cuda and torch.cuda.is_bf16_supported()
            and LooseVersion(torch.version.cuda) >= "11.0"
            and torch.distributed.is_nccl_available())

    if bf16_ready and precision_arg == 32:
        print("NOTICE: This CUDA GPU supports bfloat16 precision. We suggest you use '-float_precision 16' for faster inference.")
        #input("Press Enter to continue...")

    # we should use CPU adamW for deepspeed
    if precision_arg == 16 and bf16_ready:
        print("** bfloat16 available: enable bfloat16 training, instead of fp16.")
        precision_arg = "bf16"

    # ================ for retrieve task data ==================
    # 데이터 모듈, 이를 처리하기 위한 collator, 그리고 출력 label-id 를 mapping하는 dict를 받는다
    data_module, collator, label_id_map = task_utils.get_task_data(args.task,
                                                                   args.batch_size,
                                                                   args.tokenizer)
    if data_module is None:
        raise Exception("invalid -task option argument.")
    # ==========================================================

    model = ETRIT5ConditionalGenModelLightningModule.load_from_checkpoint(args.model)
    model.tknizer = tknizer = AutoTokenizer.from_pretrained(args.tokenizer)
    # set collator
    model.data_collator = collator

    # initialize trainer,
    # fsdp_native를 사용해야 할 경우, configure_optimizer에서
    # torch.optim.AdamW()에 self.trainer.model.parameters()가 전달되어야 함.
    # bf16을 쓰고 싶으면 ampere급 이상 GPU가 있는 곳에서 해라. 최소 A6000 필요함
    # 호스트 메모리가 적으면 nvme offloading도 고려해야 함
    # gradient accumulation을 사용하면 global_step이 그에 맞게 떨어진다.
    # learning rate scheduler를 위해서, max_epoch을 충분히 크게 잡을 것.

    trainer = pl.Trainer(accelerator="gpu",
            devices=1, num_nodes=1,
            precision=precision_arg,
            )
    trainer.test(model, datamodule=data_module)

    import multiprocessing as mp

    detokenized_preds = []
    def _decode_a_batch(grp):
        return tknizer.batch_decode(grp, skip_special_tokens=True)

    with mp.Pool(processes=8) as pool:
        print("Detokenize Prediction Output.")
        detokenized_preds = pool.map(_decode_a_batch, test_helper.INFER_PREDICTIONS)

    with mp.Pool(processes=8) as pool:
        print("Detokenize Gold Lables.")
        detokenized_lbls = pool.map(_decode_a_batch, test_helper.INFER_LABELS)

    test_helper.INFER_LABELS = [item for sublist in detokenized_lbls for item in sublist]
    test_helper.INFER_PREDICTIONS = [item for sublist in detokenized_preds for item in sublist]

    print(f"# Test Labels: {len(test_helper.INFER_LABELS)}")
    print(f"# Test Predictions: {len(test_helper.INFER_PREDICTIONS)}")

    print(f"Predicted Unique labels(will include mis-typed label elements):")
    uniq_preds = task_utils.get_unique_labels(test_helper.INFER_PREDICTIONS)
    for k, v in uniq_preds.items():
        print(f"\tlabel text: [{k}], counts: {v}")

    if label_id_map is not None:
        print("\n* trying to correct mis-typed labels with levenshtein(edit) distance.")
        correction_map = task_utils.get_mislabel_correction_map(label_id_map, uniq_preds)

        if len(correction_map) > 0:
            print("correction map:")
            for k, v in correction_map.items():
                print(f"\t{k} -> {v}")
            print("\nCORRECTED Uniq Labels and stats:")
            for idx, v in enumerate(test_helper.INFER_PREDICTIONS):
                if v in correction_map:
                    test_helper.INFER_PREDICTIONS[idx] = correction_map[v]
            corr_cnts = Counter(test_helper.INFER_PREDICTIONS)
            for k, v in corr_cnts.items():
                print(f"\tlabel text: [{k}], counts: {v}")
        else:
            print("** all tag labels are clean, so we don't need correction map. nice! **")

        # label text to label id mapping
        int_lbls = [label_id_map[x] for x in test_helper.INFER_LABELS]
        int_preds = [label_id_map[x] for x in test_helper.INFER_PREDICTIONS]

        # then calculate accuracy. FIXME: introduce f1 measure.
        acc_metric = evaluate.load("accuracy")
        results = acc_metric.compute(references=int_lbls, predictions=int_preds)
        print(results)
    else:
        print("\nWARNING: label-id map dictionary is None, so we cannot evaluate them. see task_utils.py:get_task_data()")

