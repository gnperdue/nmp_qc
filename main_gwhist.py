#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
    Trains a Neural Message Passing Model on various datasets. Methodology defined in:

    Gilmer, J., Schoenholz S.S., Riley, P.F., Vinyals, O., Dahl, G.E. (2017)
    Neural Message Passing for Quantum Chemistry.
    arXiv preprint arXiv:1704.01212 [cs.LG]
"""

# Own Modules
import datasets
from models.model import Nmp
import LogMetric
from LogMetric import AverageMeter, Logger

# Torch
import torch
import torch.optim as optim
import torch.nn as nn
from torch.autograd import Variable

import time
import argparse
import os, sys
import numpy as np

reader_folder = os.path.realpath( os.path.abspath('../GraphReader'))
if reader_folder not in sys.path:
    sys.path.insert(1, reader_folder)

from GraphReader.graph_reader import read_2cols_set_files, create_numeric_classes

__author__ = "Pau Riba, Anjan Dutta"
__email__ = "priba@cvc.uab.cat, adutta@cvc.uab.cat"

torch.multiprocessing.set_sharing_strategy('file_system')

# Parser check
def restricted_float(x, inter):
    x = float(x)
    if x < inter[0] or x > inter[1]:
        raise argparse.ArgumentTypeError("%r not in range [1e-5, 1e-4]"%(x,))
    return x

# Argument parser
parser = argparse.ArgumentParser(description='Neural message passing')

parser.add_argument('--dataset', default='gwhistograph', help='GWHISTOGRAPH')
parser.add_argument('--datasetPath', default='./data/GWHistoGraphs/', help='dataset path')
parser.add_argument('--subSet', default='01_Keypoint', help='sub dataset')
parser.add_argument('--logPath', default='./log/', help='log path')
# Optimization Options
parser.add_argument('--batch-size', type=int, default=20, metavar='N',
                    help='Input batch size for training (default: 20)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='Enables CUDA training')
parser.add_argument('--epochs', type=int, default=360, metavar='N',
                    help='Number of epochs to train (default: 360)')
parser.add_argument('--lr', type=lambda x: restricted_float(x, [1e-5, 5e-4]), default=1e-4, metavar='LR',
                    help='Initial learning rate [1e-5, 5e-4] (default: 1e-4)')
parser.add_argument('--lr-decay', type=lambda x: restricted_float(x, [.01, 1]), default=0.6, metavar='LR-DECAY',
                    help='Learning rate decay factor [.01, 1] (default: 0.6)')
parser.add_argument('--schedule', type=list, default=[0.1, 0.9], metavar='S',
                    help='Percentage of epochs to start the learning rate decay [0, 1] (default: [0.1, 0.9])')
parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                    help='SGD momentum (default: 0.9)')
# i/o
parser.add_argument('--log-interval', type=int, default=100, metavar='N',
                    help='How many batches to wait before logging training status')
# Accelerating
parser.add_argument('--prefetch', type=int, default=2, help='Pre-fetching threads.')

def main():
    global args
    args = parser.parse_args()

    # Check if CUDA is enabled
    args.cuda = not args.no_cuda and torch.cuda.is_available()

    # Load data
    root = args.datasetPath
    subset = args.subSet

    print('Prepare files')
    
    train_classes, train_ids = read_2cols_set_files(os.path.join(root,'Set/Train.txt'))
    valid_classes, valid_ids = read_2cols_set_files(os.path.join(root,'Set/Valid.txt'))
    test_classes, test_ids = read_2cols_set_files(os.path.join(root,'Set/Test.txt'))    
    
    train_classes, valid_classes, test_classes = create_numeric_classes(train_classes, valid_classes, test_classes)
    
    max_class_num = max(train_classes+valid_classes+test_classes)
    
    data_train = datasets.GWHISTOGRAPH(root, subset, train_ids, train_classes, max_class_num)
    data_valid = datasets.GWHISTOGRAPH(root, subset, valid_ids, valid_classes, max_class_num)
    data_test = datasets.GWHISTOGRAPH(root, subset, test_ids, test_classes, max_class_num)
    
    # Define model and optimizer
    print('Define model')
    # Select one graph
    g_tuple, l = data_train[0]
    g, h_t, e = g_tuple
    
    print('\tStatistics')
    stat_dict = datasets.utils.get_graph_stats(data_valid, ['degrees'])

    # Data Loader
    train_loader = torch.utils.data.DataLoader(data_train,
                                               batch_size=20, shuffle=True, collate_fn=datasets.utils.collate_g,
                                               num_workers=args.prefetch, pin_memory=True)
    valid_loader = torch.utils.data.DataLoader(data_valid,
                                               batch_size=20, shuffle=False, collate_fn=datasets.utils.collate_g,
                                               num_workers=args.prefetch, pin_memory=True)
    test_loader = torch.utils.data.DataLoader(data_test,
                                              batch_size=20, shuffle=False, collate_fn=datasets.utils.collate_g,
                                              num_workers=args.prefetch, pin_memory=True)

    print('\tCreate model')
    model = Nmp(stat_dict['degrees'], [len(h_t[0]), len(list(e.values())[0])], [25, 30, 35], len(l))

    print('Check cuda')
    if args.cuda:
        model.cuda()

    print('Optimizer')
    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    criterion = nn.MSELoss()
    evaluation = nn.L1Loss()

    print('Logger')
    logger = Logger(args.logPath)

    lr_step = (args.lr-args.lr*args.lr_decay)/(args.epochs*args.schedule[1] - args.epochs*args.schedule[0])

    # Epoch for loop
    for epoch in range(0, args.epochs):

        if epoch > args.epochs*args.schedule[0] and epoch < args.epochs*args.schedule[1]:
            args.lr -= lr_step
            for param_group in optimizer.param_groups:
                param_group['lr'] = args.lr

        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, evaluation, logger)

        # evaluate on validation set
        validate(valid_loader, model, criterion, evaluation, logger)


def train(train_loader, model, criterion, optimizer, epoch, evaluation, logger):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    error_ratio = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()
    for i, (g, h, e, target) in enumerate(train_loader):
        
        # Prepare input data
        if args.cuda:
            g, h, e, target = g.cuda(), h.cuda(), e.cuda(), target.cuda()
        g, h, e, target = Variable(g), Variable(h), Variable(e), Variable(target)

        # Measure data loading time
        data_time.update(time.time() - end)

        optimizer.zero_grad()

        # Compute output
        output = model(g, h, e)
        train_loss = criterion(output, target)

        # Logs
        losses.update(train_loss.data[0], g.size(0))
        error_ratio.update(evaluation(output, target).data[0], g.size(0))

        # compute gradient and do SGD step
        train_loss.backward()
        optimizer.step()

        # Measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.log_interval == 0:
            
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Error Ratio {err.val:.4f} ({err.avg:.4f})'
                  .format(epoch, i, len(train_loader), batch_time=batch_time,
                          data_time=data_time, loss=losses, err=error_ratio))
                          
    logger.log_value('train_epoch_loss', losses.avg)
    logger.log_value('train_epoch_error_ratio', error_ratio.avg)


def validate(val_loader, model, criterion, evaluation, logger):
    batch_time = AverageMeter()
    losses = AverageMeter()
    error_ratio = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    for i, (g, h, e, target) in enumerate(val_loader):

        # Prepare input data
        if args.cuda:
            g, h, e, target = g.cuda(), h.cuda(), e.cuda(), target.cuda()
        g, h, e, target = Variable(g), Variable(h), Variable(e), Variable(target)

        # Compute output
        output = model(g, h, e)

        # Logs
        losses.update(criterion(output, target).data[0], g.size(0))
        error_ratio.update(evaluation(output, target).data[0], g.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.log_interval == 0:
            
            print('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Error Ratio {err.val:.4f} ({err.avg:.4f})'
                  .format(i, len(val_loader), batch_time=batch_time,
                          loss=losses, err=error_ratio))

    print(' * Average Error Ratio {err.avg:.3f}'
          .format(err=error_ratio))
          
    logger.log_value('test_epoch_loss', losses.avg)
    logger.log_value('test_epoch_error_ratio', error_ratio.avg)
    logger.log_value('test_epoch_time', batch_time.avg).step()
          
    
if __name__ == '__main__':
    main()