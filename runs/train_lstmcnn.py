import os
import json
from collections import Counter

import tqdm
import time
import torch
import random
import logging
import numpy as np
from argparse import ArgumentParser

from torch.utils.data import DataLoader

from data import ConllParser, NameTaggingDataset
from model import LstmCNN
from util import counter_to_vocab, merge_vocabs, build_embedding_vocab, \
    build_form_mapping, build_signal_embed, load_vocab, \
    calculate_labeling_scores, save_result_file, calculate_lr

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s-%(levelname)s: %(message)s')
logger = logging.getLogger()

# parse commandline arguments
parser = ArgumentParser()
# i/o
parser.add_argument('-i', '--input', help='path to the input directory')
parser.add_argument('-o', '--output', help='path to the output directory')
# training parameters
parser.add_argument('--lr', type=float, default=1e-3, help='learning rate')
parser.add_argument('-b', '--batch_size', type=int, default=10)
parser.add_argument('-m', '--max_epoch', type=int, default=20)
parser.add_argument('-s', '--seed', type=int, default=1111)
parser.add_argument('--eval_step', type=int, default=-1)
# model parameters
parser.add_argument('-e', '--embed')
parser.add_argument('--embed_vocab', default=None)
parser.add_argument('--char_dim', type=int, default=25)
parser.add_argument('--word_dim', type=int, default=100)
parser.add_argument('--char_filters', default='[[2,25],[3,25],[4,25]]')
parser.add_argument('--char_feat_dim', type=int, default=100)
parser.add_argument('--lstm_size', type=int, default=100)
parser.add_argument('--lstm_dropout', type=float, default=.5)
parser.add_argument('--feat_dropout', type=float, default=.5)
parser.add_argument('--char_type', default='ffn',
                    help='ffn: feed-forward netword; hw: highway network')
# gpu
parser.add_argument('-d', '--device', type=int, default=0,
                    help='GPU device index')
args = parser.parse_args()
params = vars(args)

# timestamp
timestamp = time.strftime('%Y%m%d_%H%M%S', time.localtime())

# output
output_dir = os.path.join(args.output, timestamp)
os.mkdir(output_dir)
best_model_file = os.path.join(output_dir, 'model.best.mdl')
dev_result_file = os.path.join(output_dir, 'result.dev.bio')
test_result_file = os.path.join(output_dir, 'result.test.bio')
logger.info('Output directory: {}'.format(output_dir))

# deterministic behavior
random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)

# set gpu device
use_gpu = torch.cuda.is_available()
if use_gpu:
    torch.cuda.set_device(args.device)

# data sets
conll_parser = ConllParser([0, 1])
train_set = NameTaggingDataset(os.path.join(args.input, 'train.tsv'),
                               conll_parser, gpu=use_gpu)
dev_set = NameTaggingDataset(os.path.join(args.input, 'dev.tsv'),
                             conll_parser, gpu=use_gpu)
test_set = NameTaggingDataset(os.path.join(args.input, 'test.tsv'),
                              conll_parser, gpu=use_gpu)

# embedding
if args.embed_vocab:
    embed_vocab = load_vocab(args.embed_vocab)
else:
    embed_vocab = build_embedding_vocab(args.embed)

# vocabulary
token_vocab = load_vocab(os.path.join(args.input, 'token.vocab.tsv'))
char_vocab = load_vocab(os.path.join(args.input, 'char.vocab.tsv'))
label_vocab = load_vocab(os.path.join(args.input, 'label.vocab.tsv'))
label_itos = {i: l for l, i in label_vocab.items()}
train_token_counter = train_set.token_counter
vocabs = dict(token=token_vocab,
              char=char_vocab,
              label=label_vocab,
              embed=embed_vocab,
              form=build_form_mapping(token_vocab))
counters = dict(token=train_token_counter)

# numberize data set
train_set.numberize(vocabs)
dev_set.numberize(vocabs)
test_set.numberize(vocabs)

# create model
batch_step = len(train_set) // args.batch_size
total_step = batch_step * args.max_epoch
eval_step = batch_step if args.eval_step == -1 else args.eval_step
char_filters = json.loads(args.char_filters)
model = LstmCNN(vocabs=vocabs,
                word_embed_file=args.embed_file,
                word_embed_dim=args.word_dim,
                char_embed_dim=args.char_dim,
                char_filters=args.char_filters,
                lstm_hidden_size=args.lstm_size,
                lstm_dropout=args.lstm_dropout,
                feat_dropout=args.feat_dropout)
optimizer = torch.optim.Adam(filter(lambda x: x.requires_grad,
                                    model.parameters()),
                             lr=args.lr, weight_decay=.001)
if use_gpu:
    model.cuda()

# state
best_scores = {
    'dev': {'p': 0, 'r': 0, 'f': 0}, 'test': {'p': 0, 'r': 0, 'f': 0}}
state = dict(model=model.state_dict(),
             optimizer=optimizer.state_dict(),
             scores=best_scores,
             params=params,
             vocabs=vocabs,
             counters=counters)

# training
global_step = 0
for epoch in range(args.max_epoch):
    logger.info('Epoch: {}'.format(epoch))
    epoch_loss = []
    progress = tqdm.tqdm(desc='Epoch: {}'.format(epoch), mininterval=1,
                         total=batch_step)
    for batch in DataLoader(train_set,
                            batch_size=args.batch_size,
                            shuffle=True,
                            collate_fn=train_set.batch_processor):
        global_step += 1
        progress.update()
        optimizer.zero_grad()
        token_ids, char_ids, label_ids, seq_lens, _, _ = batch
        loglik, _ = model.forward(token_ids, char_ids, seq_lens, label_ids)
        loss = -loglik.mean()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimizer.step()
        epoch_loss.append(loss.item())

        # evaluate the model
        if global_step % eval_step == 0 or global_step == total_step:
            # dev set
            best_epoch = False
            results = []
            for batch_dev in DataLoader(dev_set, batch_size=50, shuffle=False,
                                        collate_fn=dev_set.batch_processor):
                (token_ids, char_ids, label_ids, seq_lens,
                 tokens, labels) = batch_dev
                preds = model.predict(token_ids, char_ids, seq_lens)
                preds = [[label_itos[l] for l in ls] for ls in preds]
                results.append((preds, labels, tokens, seq_lens))
            fscore, prec, rec = calculate_labeling_scores(results)
            print()
            logger.info('Dev - P: {:.2f} R: {:.2f} F: {:.2f}'.format(
                fscore, prec, rec))
            if fscore > best_scores['dev']['f']:
                best_epoch = True
                best_scores['dev'] = {'f': fscore, 'p': prec, 'r': rec}
                torch.save(state, best_model_file)
                save_result_file(results, dev_result_file)

            # test set
            results = []
            for batch_test in DataLoader(test_set, batch_size=50, shuffle=False,
                                         collate_fn=test_set.batch_processor):
                (token_ids, char_ids, label_ids, seq_lens,
                 tokens, labels) = batch_test
                preds = model.predict(token_ids, char_ids, seq_lens)
                preds = [[label_itos[l] for l in ls] for ls in preds]
                results.append((preds, labels, tokens, seq_lens))
            fscore, prec, rec = calculate_labeling_scores(results)
            logger.info('Test - P: {:.2f} R: {:.2f} F: {:.2f}'.format(
                fscore, prec, rec))
            print()
            if best_epoch:
                best_scores['test'] = {'f': fscore, 'p': prec, 'r': rec}
                save_result_file(results, test_result_file)

            lr = calculate_lr(args.lr, global_step, total_step,
                              min_lr=0.01 * args.lr)
            for p in optimizer.param_groups:
                p['lr'] = lr

    logger.info('Best dev: P: {:.2f}, R: {:.2f}, F: {:.2f}'.format(
        best_scores['dev']['p'], best_scores['dev']['r'],
        best_scores['dev']['f']))
    logger.info('Best test: P: {:.2f}, R: {:.2f}, F: {:.2f}'.format(
        best_scores['test']['p'], best_scores['test']['r'],
        best_scores['test']['f']))