#!/usr/bin/env python
# coding: utf-8
import numpy as np
import pandas as pd
from pathlib import Path
import os

import torch
import torch.optim as optim
import random

from fastai import *
from fastai.text import *
from fastai.callbacks import *

import pickle

############### Evaluation function ###############
# Find a better place for it
from evaluation import softmax, normalize
from evaluation import ndcg_at_k, precision_at_k
from evaluation import EvaluationData, loadEvaluationData
from evaluation import getMethodName, makeMDRow, EnsembleConfig, TestConfig
from evaluation import findThreshold, getMetrics, makeEnsemble, testFunction, basicEvaluation

from sklearn.metrics import precision_score, recall_score, accuracy_score, f1_score
from sklearn.metrics import classification_report

import numpy as np
from copy import copy


def performEvaluation(df, c2i, learner, vocab, LABEL_COL_NAME, COLUMNS, model_output_name):
    _ = learner.load(model_output_name)

    selected_group = [learner.data.c2i[k] for k in learner.data.c2i.keys()]

    validationDataOrg = loadEvaluationData(df, c2i, learner, vocab, LABEL_COL_NAME, selected_group, COLUMNS,
                                           split='val', original=True)
    testDataOrg = loadEvaluationData(df, c2i, learner, vocab, LABEL_COL_NAME, selected_group, COLUMNS, split='test',
                                     original=True)

    # additional labels (zero-shot)
    AdditionalColumnsLength = validationDataOrg.y_true.shape[1] - validationDataOrg.y_pred.shape[1]
    validationDataOrg.y_pred = np.concatenate(
        [validationDataOrg.y_pred, np.zeros((validationDataOrg.y_pred.shape[0], AdditionalColumnsLength))], axis=1)
    testDataOrg.y_pred = np.concatenate(
        [testDataOrg.y_pred, np.zeros((testDataOrg.y_pred.shape[0], AdditionalColumnsLength))], axis=1)

    report, f1_val = basicEvaluation(validationDataOrg, testDataOrg, EnsembleConfig, plot=True, **TestConfig)

    prAtK = []
    for k in range(1, 20):
        prAtK.append(precision_at_k(testDataOrg.y_true, testDataOrg.y_pred, k))

    nDcgAtK = []
    for k in range(1, 20):
        nDcgAtK.append(ndcg_at_k(testDataOrg.y_true, testDataOrg.y_pred, k))

    return f1_val, prAtK, nDcgAtK


#### Find a way to move out these functions ####
def seed_all(seed_value):
    random.seed(seed_value)
    np.random.seed(seed_value)
    torch.manual_seed(seed_value)

    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed_value)
        torch.cuda.manual_seed_all(seed_value)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


def performCycle(learner, unfreeze_to, n_iterations, max_lr, model_name, continue_from, seed=42):
    seed_all(seed)
    learner.load("{}/{}".format(model_name, continue_from - 1))
    if unfreeze_to < 0:
        learner.freeze_to(unfreeze_to)
    learner.fit_one_cycle(n_iterations, max_lr=max_lr, moms=(0.8, 0.7))
    learner.save("{}/{}".format(model_name, continue_from))

    return learner


def lr_find(learner, unfreeze_to, n_iterations, max_lr, model_name, continue_from, seed=42,
            figname="lrFind/default.png"):
    seed_all(seed)
    learner.load("{}/{}".format(model_name, continue_from - 1))
    if unfreeze_to < 0:
        learner.freeze_to(unfreeze_to)
    learner.lr_find()

    plt.figure()
    learner.recorder.plot(skip_end=10, suggestion=True)
    plt.savefig(figname)

########## CONSTANTS
TEXT_FIELD = 'text'
SPLIT_FIELD = 'split'
FILE_ID_FIELD = 'celex_id'
TRAIN_LABEL = 'train'
VALIDATION_LABEL = 'val'
TEST_LABEL = 'test'
NO_SPLIT_LABEL = 'no split'
LABEL_DELIM = ';'
TRAIN_FILENAME = 'train.txt'
VALIDATION_FILENAME = 'validation.txt'
TEST_FILENAME = 'test.txt'

def prepareDataset(dataset_path, datasetSplit, uncased):
    def getSplit(celex_id, trainset, valset, testset):
        if celex_id in trainset:
            return TRAIN_LABEL
        elif celex_id in valset:
            return VALIDATION_LABEL
        elif celex_id in testset:
            return TEST_LABEL
        else:
            return NO_SPLIT_LABEL    
    # Load dataset
    data = pd.read_csv(dataset_path)
    if uncased:
        data[TEXT_FIELD] = data[TEXT_FIELD].apply(lambda x: x.lower())
    if len(datasetSplit)>0:
        try:
            with open(datasetSplit + '/' + TRAIN_FILENAME) as fin:
                trainset = [line.strip() for line in fin]
            with open(datasetSplit + '/' + VALIDATION_FILENAME) as fin:
                valset = [line.strip() for line in fin]
            with open(datasetSplit + '/' + TEST_FILENAME) as fin:
                testset = [line.strip() for line in fin]
            data[SPLIT_FIELD] = data[FILE_ID_FIELD].apply(lambda w: getSplit(w, trainset, valset, testset))
        except Exception as ex:
            print(ex)
    return data
###############################################

import argparse

parser = argparse.ArgumentParser("Finetune bert for multi-label classification")

## Data
parser.add_argument("--dataset_path", help="path of the dataset", type=str)
parser.add_argument("--dataset_split_path", default="", help="path of the dataset", type=str)
parser.add_argument("--LABEL_COL_NAME", default='Labels', help="Labels, Domain, MThesaurus, Topterm", type=str)
parser.add_argument('--cased', default=0, help="set 1 if the model is cased", type=int)

## Architecture
parser.add_argument("--model_type", default='bert', help="model type", type=str)
parser.add_argument("--pretrained_model_name", default='bert-base-uncased', help="model name or path", type=str)
parser.add_argument("--MAX_LEN", default=512, help="Max sequence len", type=int)

## Configuration
parser.add_argument("--BATCH_SIZE", default=4, help="Batch size", type=int)
parser.add_argument("--TOTAL_CYCLES", default=3, help="total number of cycles", type=int)
parser.add_argument("--START_CYCLE", default=1, help="Start/continue training from this cycle", type=int)
parser.add_argument("--N_ITERATIONS", default="12,12,12", help="number of iterations per cycle", type=str)
parser.add_argument("--LR", default="2e-04,5e-05,5e-06",
                    help="max learning rate for each cycle (last one will be default for the rest cycles)", type=str)
parser.add_argument("--UNFREEZED", default="-4,-8,-12",
                    help="unfreezed layers per cycle (last one will be default for the rest cycles)", type=str)

parser.add_argument("--experiment_name", help="name of the output model", type=str)
parser.add_argument('--lr_find', default=0, help="set to 1 to find learning-rate", type=int)

args = parser.parse_args()
for arg in vars(args):
    print(arg, getattr(args, arg))

## Data
dataset_path = args.dataset_path
dataset_split_path = args.dataset_split_path
LABEL_COL_NAME = args.LABEL_COL_NAME
uncased = (args.cased == 0)

## Architecture
model_type = args.model_type
pretrained_model_name = args.pretrained_model_name
MAX_LEN = args.MAX_LEN

## Configuration
bs = args.BATCH_SIZE
TOTAL_CYCLES = args.TOTAL_CYCLES
START_CYCLE = args.START_CYCLE
N_ITERATIONS = [int(value) for value in args.N_ITERATIONS.split(',')]
LR = [float(value) for value in args.LR.split(',')]
UNFREEZE = [int(value) for value in args.UNFREEZED.split(',')]

experiment_name = args.experiment_name
LR_FIND = (args.lr_find == 1)

# Parameters
seed = 42
use_fp16 = False
pad_first = bool(model_type in ['xlnet'])

assert LABEL_COL_NAME in ['MThesaurus', 'Domain', 'Topterm', 'Labels', 'ExtDesc', 'Domains', 'Descriptors']
#######################################

## Create output dir for models
MODEL_PATH = "models/{}".format(experiment_name)
LR_PATH = "experiments/{}/lrFind/".format(experiment_name)
EXPERIMENT_PATH = "experiments/{}/".format(experiment_name)
logfilename = EXPERIMENT_PATH + "/logs"

Path(MODEL_PATH).mkdir(parents=True, exist_ok=True)
Path(LR_PATH).mkdir(parents=True, exist_ok=True)


# Load dataset
df = prepareDataset(dataset_path, dataset_split_path, uncased)
testDf = df[df[SPLIT_FIELD] == TEST_LABEL]

# Load Transformer model
from transformersmd import MODEL_CLASSES, getTransformerProcecssor, CustomTransformerModel, getListLayersBert, \
    getLearner

model_class, tokenizer_class, config_class = MODEL_CLASSES[model_type]
transformer_processor = getTransformerProcecssor(tokenizer_class, pretrained_model_name, model_type, maxlen=MAX_LEN)
pad_idx = transformer_processor[1].vocab.tokenizer.pad_token_id

# prepare classification data
train_idx = list(df[df[SPLIT_FIELD] == TRAIN_LABEL].index)
valid_idx = list(df[df[SPLIT_FIELD] == VALIDATION_LABEL].index)

data_clas = (TextList.from_df(df, processor=transformer_processor, cols=TEXT_FIELD)
             .split_by_idxs(train_idx, valid_idx)
             .label_from_df(cols=LABEL_COL_NAME, label_delim=LABEL_DELIM)
             .databunch(bs=bs, pad_first=pad_first, pad_idx=pad_idx))

if LR_FIND:
    logfilename = None

learner = getLearner(data_clas, pretrained_model_name, model_class, config_class, use_fp16, logfilename=logfilename,
                     append=True, model_type=model_type)
test_data = TextList.from_df(testDf, cols='text', vocab=learner.data.vocab)
learner.data.add_test(test_data)

learner.save("{}/{}".format(experiment_name, 0))
print('done')

# Task settings (I can get rid of them)
c2i = learner.data.c2i
COLUMNS = list(learner.data.classes)
vocab = learner.data.vocab

## update c2i  ########### Load tests from the very beginning ############
print("len c2i before", len(c2i))
for i in range(len(df)):
    labels_raw = df[LABEL_COL_NAME].iloc[i]
    for singlelabel in labels_raw.split(';'):
        if singlelabel not in c2i.keys():
            c2i[singlelabel] = len(c2i)
            COLUMNS.append(singlelabel)
print("len c2i after", len(c2i))
###############################################

for cycle in range(START_CYCLE, TOTAL_CYCLES + 1):
    if cycle - 1 < len(LR):
        max_lr = LR[cycle - 1]
    else:
        max_lr = LR[-1]

    if cycle - 1 < len(UNFREEZE):
        unfreeze_to = UNFREEZE[cycle - 1]
    else:
        unfreeze_to = UNFREEZE[-1]

    if cycle - 1 < len(N_ITERATIONS):
        n_iterations = N_ITERATIONS[cycle - 1]
    else:
        n_iterations = N_ITERATIONS[-1]

    figname = "{}/{}.png".format(LR_PATH, cycle)

    lr_find(learner, unfreeze_to, n_iterations, max_lr, experiment_name, cycle, seed=seed, figname=figname)
    if not LR_FIND:
        learner = performCycle(learner, unfreeze_to, n_iterations, max_lr, experiment_name, cycle, seed=seed)

    # Evaluation
    lastSavedModel = experiment_name + "/" + str(cycle)
    f1_val, prAtK, nDcgAtK = performEvaluation(df, c2i, learner, vocab, LABEL_COL_NAME
                                               , COLUMNS, lastSavedModel)
    currentResults = [f1_val] + prAtK + nDcgAtK
    with open(EXPERIMENT_PATH+'/results.csv', 'a') as fout:
        fout.write(','.join([str(element) for element in currentResults]) + "\n")
