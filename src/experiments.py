"""
 Copyright (c) 2020, salesforce.com, inc.
 All rights reserved.
 SPDX-License-Identifier: BSD-3-Clause
 For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause

 Experiment Portal.
"""
import random
import json
import os
import sys

import src.common.ops as ops
import src.data_processor.data_loader as data_loader
import src.data_processor.processor_utils as data_utils
from src.data_processor.data_processor import preprocess
from src.data_processor.vocab_processor import build_vocab
from src.data_processor.schema_graph import SchemaGraph
from src.data_processor.path_utils import get_model_dir, get_checkpoint_path
from src.demos.demos import Text2SQLWrapper
import src.eval.eval_tools as eval_tools
from src.eval.wikisql.lib.dbengine import DBEngine
from src.semantic_parser.learn_framework import EncoderDecoderLFramework
from src.trans_checker.args import args as cs_args
import src.utils.utils as utils

from src.parse_args import args

import torch
if not args.data_parallel:
    torch.cuda.set_device('cuda:{}'.format(args.gpu))
torch.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Set model ID
args.model_id = utils.model_index[args.model]
assert(args.model_id is not None)


def train(sp):
    dataset = data_loader.load_processed_data(args)
    train_data = dataset['train']
    print('{} training examples loaded'.format(len(train_data)))
    dev_data = dataset['dev']
    print('{} dev examples loaded'.format(len(dev_data)))

    if args.xavier_initialization:
        ops.initialize_module(sp.mdl, 'xavier')
    else:
        raise NotImplementedError

    sp.schema_graphs = dataset['schema']
    if args.checkpoint_path is not None:
        sp.load_checkpoint(args.checkpoint_path)

    if args.test:
        train_data = train_data + dev_data

    sp.run_train(train_data, dev_data)


def inference(sp):
    dataset = data_loader.load_processed_data(args)
    split = 'test' if args.test else 'dev'
    if args.dataset_name == 'wikisql':
        engine_path = os.path.join(args.data_dir, '{}.db'.format(split))
        engine = DBEngine(engine_path)
    else:
        engine = None

    def evaluate(examples, out_dict):
        metrics = eval_tools.get_exact_match_metrics(examples, out_dict['pred_decoded'], engine=engine)
        print('Top-1 exact match: {:.3f}'.format(metrics['top_1_em']))
        print('Top-2 exact match: {:.3f}'.format(metrics['top_2_em']))
        print('Top-3 exact match: {:.3f}'.format(metrics['top_3_em']))
        print('Top-5 exact match: {:.3f}'.format(metrics['top_5_em']))
        print('Top-10 exact match: {:.3f}'.format(metrics['top_10_em']))
        if args.dataset_name == 'wikisql':
            print('Top-1 exe match: {:.3f}'.format(metrics['top_1_ex']))
            print('Top-2 exe match: {:.3f}'.format(metrics['top_2_ex']))
            print('Top-3 exe match: {:.3f}'.format(metrics['top_3_ex']))
            print('Top-5 exe match: {:.3f}'.format(metrics['top_5_ex']))
            print('Top-10 exet match: {:.3f}'.format(metrics['top_10_ex']))
        print('Table error: {:.3f}'.format(metrics['table_err']))

    examples = dataset[split]
    # random.shuffle(examples)
    sp.schema_graphs = dataset['schema']
    print('{} {} examples loaded'.format(len(examples), split))

    if sp.args.use_pred_tables:
        in_table = os.path.join(sp.args.model_dir, 'predicted_tables.txt')
        with open(in_table) as f:
            content = f.readlines()
        assert(len(content) == len(examples))
        for example, line in zip(examples, content):
            pred_tables = set([x.strip()[1:-1] for x in line.strip()[1:-1].split(',')])
            example.leaf_condition_vals_list = pred_tables

    sp.load_checkpoint(get_checkpoint_path(args))
    sp.eval()

    if sp.args.augment_with_wikisql:
        examples_, examples_wikisql = [], []
        for example in examples:
            if example.dataset_id == data_utils.WIKISQL:
                examples_wikisql.append(example)
            else:
                examples_.append(example)
        examples = examples_

    pred_restored_cache = sp.load_pred_restored_cache()
    pred_restored_cache_size = sum(len(v) for v in pred_restored_cache.values())
    pred_restored_cache = None
    out_dict = sp.inference(examples, restore_clause_order=args.process_sql_in_execution_order,
                            pred_restored_cache=pred_restored_cache,
                            check_schema_consistency_=args.sql_consistency_check,
                            engine=engine, inline_eval=True, verbose=True)
    if args.process_sql_in_execution_order:
        new_pred_restored_cache_size = sum(len(v) for v in out_dict['pred_restored_cache'].values())
        newly_cached_size = new_pred_restored_cache_size - pred_restored_cache_size
        if newly_cached_size > 0:
            sp.save_pred_restored_cache(out_dict['pred_restored_cache'], newly_cached_size)

    out_txt = os.path.join(sp.model_dir, 'predictions.{}.{}.{}.txt'.format(args.beam_size, args.bs_alpha, split))
    with open(out_txt, 'w') as o_f:
        assert(len(examples) == len(out_dict['pred_decoded']))
        for i, pred_sql in enumerate(out_dict['pred_decoded']):
            if args.dataset_name == 'wikisql':
                example = examples[i]
                o_f.write('{}\n'.format(json.dumps(
                    {'sql': pred_sql[0], 'table_id': example.db_name})))
            else:
                o_f.write('{}\n'.format(pred_sql[0]))
        print('Model predictions saved to {}'.format(out_txt))

    print('{} set performance'.format(split.upper()))
    evaluate(examples, out_dict)
    if args.augment_with_wikisql:
        wikisql_out_dict = sp.forward(examples_wikisql, verbose=False)
        print('*** WikiSQL ***')
        evaluate(examples_wikisql, wikisql_out_dict)


def ensemble():
    dataset = data_loader.load_processed_data(args)
    split = 'test' if args.test else 'dev'
    dev_examples = dataset[split]
    print('{} dev examples loaded'.format(len(dev_examples)))
    if args.dataset_name == 'wikisql':
        engine_path = os.path.join(args.data_dir, '{}.db'.format(split))
        engine = DBEngine(engine_path)
    else:
        engine = None

    model_dirs = [
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201113-113558.vgs0/model-best.16.tar',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201113-113558.ljap/model-best.16.tar',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201113-115517.51x5/model-best.16.tar',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.1-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201112-185010.0rip/model-best.16.tar',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.1-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201112-185458.gryi/model-best.16.tar',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.1-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201112-184443.ss5p/model-best.16.tar'
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201128-210933.j6ot/model-best.16.tar',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201128-210906.yi82/model-best.16.tar',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201128-210911.1ngi/model-best.16.tar'
        # 'model/model_analysis/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-384-384-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201216-111910.c73s',
        # 'model/model_analysis/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-384-384-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201216-111910.g965',
        # 'model/model_analysis/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-384-384-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201216-111910.o6jm',
        # 'model/model_analysis/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-384-384-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201216-111913.hptm'
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-093155.gjrk/',
        # 70.1, 68.2
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-093255.vn2k/',
        # 69.1, 67.1
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-110818.d08k/',
        # 68.4, 67.5
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-093110.a97i/',
        # 68.3, 66.2
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-093153.xf80/',
        # 68.2, 67.6
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-132607.nj9q/',
        # 68.2, 67.4
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-093309.46gg/',
        # 67.7, 67.4
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-093116.8vxb/',
        # 67.2, 67.0
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-093640.ec17/',
        # 67.2, 66.4
        'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-093120.3un8/',
        # 66.7, 66.0
        # 'model/ensemble_analysis/spider.bridge.lstm.meta.ts.ppl-0.85.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201218-093133.lmya/',
        # 66.4, 65.8
    ]

    sps = [EncoderDecoderLFramework(args) for _ in model_dirs]
    for i, model_dir in enumerate(model_dirs):
        checkpoint_path = os.path.join(model_dir, 'model-best.16.tar')
        sps[i].schema_graphs = dataset['schema']
        sps[i].load_checkpoint(checkpoint_path)
        sps[i].cuda()
        sps[i].eval()

    pred_restored_cache = sps[0].load_pred_restored_cache()
    pred_restored_cache_size = sum(len(v) for v in pred_restored_cache.values())

    out_dict = sps[0].inference(dev_examples, restore_clause_order=args.process_sql_in_execution_order,
                                pred_restored_cache=pred_restored_cache,
                                check_schema_consistency_=args.sql_consistency_check, engine=engine,
                                inline_eval=True, model_ensemble=[sp.mdl for sp in sps], verbose=True)

    if args.process_sql_in_execution_order:
        new_pred_restored_cache_size = sum(len(v) for v in out_dict['pred_restored_cache'].values())
        newly_cached_size = new_pred_restored_cache_size - pred_restored_cache_size
        if newly_cached_size > 0:
            sps[0].save_pred_restored_cache(out_dict['pred_restored_cache'], newly_cached_size)

    out_txt = os.path.join(sps[0].model_dir, 'predictions.ens.{}.{}.{}.{}.txt'.format(args.beam_size, args.bs_alpha, split, len(model_dirs)))
    with open(out_txt, 'w') as o_f:
        assert(len(dev_examples) == len(out_dict['pred_decoded']))
        for i, pred_sql in enumerate(out_dict['pred_decoded']):
            if args.dataset_name == 'wikisql':
                example = dev_examples[i]
                o_f.write('{}\n'.format(json.dumps(
                    {'sql': pred_sql[0], 'table_id': example.db_name})))
            else:
                o_f.write('{}\n'.format(pred_sql[0]))
        print('Model predictions saved to {}'.format(out_txt))

    print('{} set performance'.format(split.upper()))
    metrics = eval_tools.get_exact_match_metrics(dev_examples, out_dict['pred_decoded'], engine=engine)
    print('Top-1 exact match: {:.3f}'.format(metrics['top_1_em']))
    print('Top-2 exact match: {:.3f}'.format(metrics['top_2_em']))
    print('Top-3 exact match: {:.3f}'.format(metrics['top_3_em']))
    print('Top-5 exact match: {:.3f}'.format(metrics['top_5_em']))
    print('Top-10 exact match: {:.3f}'.format(metrics['top_10_em']))


def error_analysis(sp):
    dataset = data_loader.load_processed_data(args)
    dev_examples = dataset['dev']
    sp.schema_graphs = dataset['schema']
    print('{} dev examples loaded'.format(len(dev_examples)))

    model_dirs =  [
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201113-113558.vgs0',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201113-113558.ljap',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201113-115517.51x5',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.1-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201112-185458.gryi',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.1-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201112-185010.0rip',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.1-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201112-184443.ss5p'
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201128-210933.j6ot',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201128-210906.yi82',
        # 'model/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-400-400-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201128-210911.1ngi'
        'model/model_analysis/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-384-384-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201216-111910.c73s',
        'model/model_analysis/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-384-384-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201216-111910.g965',
        'model/model_analysis/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-384-384-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201216-111910.o6jm',
        'model/model_analysis/spider.bridge.ppl.2.dn.eo.feat.bert-large-uncased.xavier-1024-384-384-16-2-0.0005-inv-sqr-0.0005-4000-6e-05-inv-sqr-3e-05-4000-0.3-0.3-0.0-0.0-1-8-0.0-0.0-res-0.2-0.0-ff-0.4-0.0.201216-111913.hptm'
    ]
    if len(model_dirs) <= 2:
        print('Needs at least 3 models to perform majority vote')
        sys.exit()

    predictions = []
    for model_dir in model_dirs:
        pred_file = os.path.join(model_dir, 'predictions.16.txt')
        with open(pred_file) as f:
            predictions.append([x.strip() for x in f.readlines()])
    for i in range(len(predictions)):
        assert(len(dev_examples) == len(predictions[i]))
  
    import collections 
    disagree = collections.defaultdict(lambda: collections.defaultdict(list))
    out_txt = 'majority_vote.txt'
    o_f = open(out_txt, 'w')
    for e_id in range(len(dev_examples)):
        example = dev_examples[e_id]
        gt_program_list = example.program_list
        votes = collections.defaultdict(list)
        for i in range(len(predictions)):
            pred_sql = predictions[i][e_id]
            votes[pred_sql].append(i)
        # break ties
        voting_results = sorted(votes.items(), key=lambda x:len(x[1]), reverse=True)
        voted_sql = voting_results[0][0]
        # TODO: the implementation below cheated
        # if len(voting_results) == 1:
        #     voted_sql = voting_results[0][0]
        # else:
        #     if len(voting_results[0][1]) > len(voting_results[1][1]):
        #         voted_sql = voting_results[0][0]
        #     else:
        #         j = 1
        #         while(j < len(voting_results) and len(voting_results[j][1]) == len(voting_results[0][1])):
        #             j += 1
        #         voting_results = sorted(voting_results[:j], key=lambda x:sum(x[1]))
        #         voted_sql = voting_results[0][0]
        o_f.write(voted_sql + '\n') 
        evals = []
        for i in range(len(predictions)):
            eval_results, _, _ = eval_tools.eval_prediction(
                pred=predictions[i][e_id],
                gt_list=gt_program_list,
                dataset_id=example.dataset_id,
                db_name=example.db_name,
                in_execution_order=False
            )
            evals.append(eval_results)
        models_agree = (len(set(evals)) == 1)
        if not models_agree:
            for i in range(len(evals)-1):
                for j in range(1, len(evals)):
                    if evals[i] != evals[j]:
                        disagree[i][j].append(e_id)
            schema = sp.schema_graphs[example.db_name]
            print('Example {}'.format(e_id+1))
            example.pretty_print(schema)
            for i in range(len(predictions)):
                print('Prediction {} [{}]: {}'.format(i+1, evals[i], predictions[i][e_id]))
            print()
    o_f.close()

    for i in range(len(predictions)-1):
        for j in range(i+1, len(predictions)):
            print('Disagree {}, {}: {}'.format(i+1, j+1, len(disagree[i][j])))
    import functools
    disagree_all = functools.reduce(lambda x, y: x & y, [set(l) for l in [disagree[i][j] for i in range(len(disagree)) for j in disagree[i]]])
    print('Disagree all: {}'.format(len(disagree_all)))
    print('Majority voting results saved to {}'.format(out_txt))

def fine_tune(sp):
    dataset = data_loader.load_processed_data(args)
    fine_tune_data = dataset['fine-tune']

    print('{} fine-tuning examples loaded'.format(len(fine_tune_data)))
    dev_data = fine_tune_data

    sp.schema_graphs = dataset['schema']
    sp.load_checkpoint(get_checkpoint_path(args))

    sp.run_train(fine_tune_data, dev_data)


def process_data():
    """
    Data preprocess.

    1. Build vocabulary.
    2. Vectorize data.
    """
    if args.dataset_name == 'spider':
        dataset = data_loader.load_data_spider(args)
    elif args.dataset_name == 'wikisql':
        dataset = data_loader.load_data_wikisql(args)
    else:
        dataset = data_loader.load_data_by_split(args)

    # build_vocab(args, dataset, dataset['schema'])
    preprocess(args, dataset, verbose=True)


def demo(args):
    data_dir = 'data/'
    db_name = 'pets_1'
    db_path = os.path.join(args.db_dir, db_name, '{}.sqlite'.format(db_name))
    schema = SchemaGraph(db_name, db_path=db_path)
    if db_name == 'covid_19':
        in_csv = os.path.join(data_dir, db_name, '{}.csv'.format(db_name))
        in_type = os.path.join(data_dir, db_name, '{}.types'.format(db_name))
        schema.load_data_from_csv_file(in_csv, in_type)
    else:
        import json
        in_json = os.path.join(args.data_dir, 'tables.json')
        with open(in_json) as f:
            tables = json.load(f)
        for table in tables:
            if table['db_id'] == db_name:
                break
        schema.load_data_from_spider_json(table)
    schema.pretty_print()
    t2sql = Text2SQLWrapper(args, cs_args, schema)

    sys.stdout.write('Enter a natural language question: ')
    sys.stdout.write('> ')
    sys.stdout.flush()
    text = sys.stdin.readline()

    while text:
        output = t2sql.process(text, schema.name)
        translatable = output['translatable']
        sql_query = output['sql_query']
        confusion_span = output['confuse_span']
        replacement_span = output['replace_span']
        print('Translatable: {}'.format(translatable))
        print('SQL: {}'.format(sql_query))
        print('Confusion span: {}'.format(confusion_span))
        print('Replacement span: {}'.format(replacement_span))
        sys.stdout.flush()
        sys.stdout.write('\nEnter a natural language question: ')
        sys.stdout.write('> ')
        text = sys.stdin.readline()


def run_experiment(args):
    if args.process_data:
        process_data()
    elif args.ensemble_inference:
        get_model_dir(args)
        assert(args.model in ['bridge',
                              'seq2seq',
                              'seq2seq.pg'])
        ensemble()
    else:
        with torch.set_grad_enabled(args.train or args.search_random_seed or args.grid_search or args.fine_tune):
            get_model_dir(args)
            if args.model in ['bridge',
                              'seq2seq',
                              'seq2seq.pg']:
                sp = EncoderDecoderLFramework(args)
            else:
                raise NotImplementedError

            sp.cuda()
            if args.train:
                train(sp)
            elif args.inference:
                inference(sp)
            elif args.error_analysis:
                error_analysis(sp)
            elif args.demo:
                demo(args)
            elif args.fine_tune:
                fine_tune(sp)
            else:
                print('No experiment specified. Exit now.')
                sys.exit(1)


if __name__ == '__main__':
    run_experiment(args)
