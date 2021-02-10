import argparse
import sys
import os
import shutil
import time
import math
import h5py

import torch
import torch.nn as nn
import torch.optim
import torchvision.transforms as transforms
import torch.nn.functional as F

import torch.nn.parallel
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

import numpy as np
import matplotlib.pyplot as plt

sys.path.append('../ResNet')
import ResNet1d as rn
sys.path.append('../')
import Model_Util
import Utilities
from Dataset_Management import Artificial_DataLoader

def parse():

    model_names = ['ResNet10', 'ResNet18', 'ResNet34', 'ResNet50', 'ResNet101', 'ResNet152']

    parser = argparse.ArgumentParser(description='Nanopore Translocation Feature Prediction Training')
    parser.add_argument('data', metavar='DIR', type=str,
                        help='path to validation dataset')
    parser.add_argument('counter', metavar='COUNTER', type=str,
                        help='path to translocation counter')
    parser.add_argument('predictor', metavar='PREDICTOR', type=str,
                        help='path to translocation feature predictor')
    parser.add_argument('--arch_1', '-a_1', metavar='ARCH_1', default='ResNet18',
                        choices=model_names,
                        help='model architecture for translocation counter: ' +
                        ' | '.join(model_names) +
                        ' (default: ResNet18)')
    parser.add_argument('--arch_2', '-a_2', metavar='ARCH_2', default='ResNet18',
                        choices=model_names,
                        help='model architecture for translocation feature predictions: ' +
                        ' | '.join(model_names) +
                        ' (default: ResNet18_Custom)')
    parser.add_argument('-b', '--batch-size', default=6, type=int,
                        metavar='N', help='mini-batch size per process (default: 6)')
    parser.add_argument('--print-freq', '-p', default=10, type=int,
                        metavar='N', help='print frequency (default: 10)')
    parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                        help='evaluate model on validation set')
    parser.add_argument('-stats', '--statistics', dest='statistics', action='store_true',
                        help='Compute statistics about errors of a trained model on validation set')
    parser.add_argument('-r', '--run', dest='run', action='store_true',
                        help='Run a trained model and plots a batch of predictions in noisy signals')
    parser.add_argument("--local_rank", default=0, type=int)
    parser.add_argument('--cpu', action='store_true',
                        help='Runs CPU based version of the workflow.')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='provides additional details as to what the program is doing')
    parser.add_argument('-t', '--test', action='store_true',
                        help='Launch test mode with preset arguments')
    parser.add_argument('-pth', '--plot-training-history', action='store_true',
                        help='Only plots the training history of a trained model: Loss and validation errors')

    args = parser.parse_args()
    return args


def main():
    global best_error, args
    best_error = math.inf
    args = parse()


    if not len(args.data):
        raise Exception("error: No data set provided")

    if not len(args.counter):
        raise Exception("error: No path to counter model provided")

    if not len(args.predictor):
        raise Exception("error: No path to predictor model provided")


    args.distributed = False
    if 'WORLD_SIZE' in os.environ:
        args.distributed = int(os.environ['WORLD_SIZE']) > 1

    args.gpu = 0
    args.world_size = 1

    if args.distributed:
        args.gpu = args.local_rank

        if not args.cpu:
            torch.cuda.set_device(args.gpu)

        torch.distributed.init_process_group(backend='gloo',
                                             init_method='env://')
        args.world_size = torch.distributed.get_world_size()

    args.total_batch_size = args.world_size * args.batch_size

    # Set the device
    device = torch.device('cpu' if args.cpu else 'cuda:' + str(args.gpu))

    # create model_1
    if args.test:
        args.arch_1 = 'ResNet10'

    if args.local_rank==0:
        print("=> creating model_1 '{}'".format(args.arch_1))

    if args.arch_1 == 'ResNet18':
        model_1 = rn.ResNet18_Counter()
    elif args.arch_1 == 'ResNet34':
        model_1 = rn.ResNet34_Counter()
    elif args.arch_1 == 'ResNet50':
        model_1 = rn.ResNet50_Counter()
    elif args.arch_1 == 'ResNet101':
        model_1 = rn.ResNet101_Counter()
    elif args.arch_1 == 'ResNet152':
        model_1 = rn.ResNet152_Counter()
    elif args.arch_1 == 'ResNet10':
        model_1 = rn.ResNet10_Counter()
    else:
        print("Unrecognized {} for translocations counter architecture" .format(args.arch_1))

    # create model_2
    if args.test:
        args.arch_2 = 'ResNet10'

    if args.local_rank==0:
        print("=> creating model_2 '{}'".format(args.arch_2))

    if args.arch_2 == 'ResNet18':
        model_2 = rn.ResNet18_Custom()
    elif args.arch_2 == 'ResNet34':
        model_2 = rn.ResNet34_Custom()
    elif args.arch_2 == 'ResNet50':
        model_2 = rn.ResNet50_Custom()
    elif args.arch_2 == 'ResNet101':
        model_2 = rn.ResNet101_Custom()
    elif args.arch_2 == 'ResNet152':
        model_2 = rn.ResNet152_Custom()
    elif args.arch_2 == 'ResNet10':
        model_2 = rn.ResNet10_Custom()
    else:
        print("Unrecognized {} for translocation feature prediction architecture" .format(args.arch_2))



    model_1 = model_1.to(device)
    model_2 = model_2.to(device)

 
    # For distributed training, wrap the model with torch.nn.parallel.DistributedDataParallel.
    if args.distributed:
        if args.cpu:
            model_1 = DDP(model_1)
            model_2 = DDP(model_2)
        else:
            model_1 = DDP(model_1, device_ids=[args.gpu], output_device=args.gpu)
            model_2 = DDP(model_2, device_ids=[args.gpu], output_device=args.gpu)

        if args.verbose:
            print('Since we are in a distributed setting the model is replicated here in local rank {}'
                                    .format(args.local_rank))


    total_time = Utilities.AverageMeter()

    # bring counter from a checkpoint
    if args.counter:
        # Use a local scope to avoid dangling references
        def bring_counter():
            if os.path.isfile(args.counter):
                print("=> loading counter '{}'" .format(args.counter))
                if args.cpu:
                    checkpoint = torch.load(args.counter, map_location='cpu')
                else:
                    checkpoint = torch.load(args.counter, map_location = lambda storage, loc: storage.cuda(args.gpu))

                loss_history_1 = checkpoint['loss_history']
                counter_error_history = checkpoint['Counter_error_history']
                best_error_1 = checkpoint['best_error']
                model_1.load_state_dict(checkpoint['state_dict'])
                total_time_1 = checkpoint['total_time']
                print("=> loaded counter '{}' (epoch {})"
                                .format(args.counter, checkpoint['epoch']))
                print("Model best precision saved was {}" .format(best_error_1))
                return best_error_1, model_1, loss_history_1, counter_error_history, total_time_1
            else:
                print("=> no counter found at '{}'" .format(args.counter))
    
        best_error_1, model_1, loss_history_1, counter_error_history, total_time_1 = bring_counter()
    else:
        raise Exception("error: No counter path provided")




    # bring predictor from a checkpoint
    if args.predictor:
        # Use a local scope to avoid dangling references
        def bring_predictor():
            if os.path.isfile(args.predictor):
                print("=> loading predictor '{}'" .format(args.predictor))
                if args.cpu:
                    checkpoint = torch.load(args.predictor, map_location='cpu')
                else:
                    checkpoint = torch.load(args.predictor, map_location = lambda storage, loc: storage.cuda(args.gpu))

                loss_history_2 = checkpoint['loss_history']
                duration_error_history = checkpoint['duration_error_history']
                amplitude_error_history = checkpoint['amplitude_error_history']
                best_error_2 = checkpoint['best_error']
                model_2.load_state_dict(checkpoint['state_dict'])
                total_time_2 = checkpoint['total_time']
                print("=> loaded predictor '{}' (epoch {})"
                                .format(args.predictor, checkpoint['epoch']))
                print("Model best precision saved was {}" .format(best_error_2))
                return best_error_2, model_2, loss_history_2, duration_error_history, amplitude_error_history, total_time_2 
            else:
                print("=> no predictor found at '{}'" .format(args.predictor))

        best_error_2, model_2, loss_history_2, duration_error_history, amplitude_error_history, total_time_2 = bring_predictor()
    else:
        raise Exception("error: No predictor path provided")


    # Data loading code
    valdir = os.path.join(args.data, 'val')

    if args.test:
        validation_f = h5py.File(valdir + '/validation_toy.h5', 'r')
    else:
        validation_f = h5py.File(valdir + '/validation.h5', 'r')


    # this is the dataset for validating
    sampling_rate = 10000                   # This is the number of samples per second of the signals in the dataset
    if args.test:
        number_of_concentrations = 2        # This is the number of different concentrations in the dataset
        number_of_durations = 2             # This is the number of different translocation durations per concentration in the dataset
        number_of_diameters = 4             # This is the number of different translocation durations per concentration in the dataset
        window = 0.5                        # This is the time window in seconds
        length = 10                         # This is the time of a complete signal for certain concentration and duration
    else:
        number_of_concentrations = 20       # This is the number of different concentrations in the dataset
        number_of_durations = 5             # This is the number of different translocation durations per concentration in the dataset
        number_of_diameters = 15            # This is the number of different translocation durations per concentration in the dataset
        window = 0.5                        # This is the time window in seconds
        length = 10                         # This is the time of a complete signal for certain concentration and duration

    # Validating Artificial Data Loader
    VADL = Artificial_DataLoader(args.world_size, args.local_rank, device, validation_f, sampling_rate,
                                 number_of_concentrations, number_of_durations, number_of_diameters,
                                 window, length, args.batch_size)

    if args.verbose:
        print('From rank {} validation shard size is {}'. format(args.local_rank, VADL.get_number_of_avail_windows()))


    if args.run:
        arguments = {'model_1': model_1,
                     'model_2': model_2,
                     'device': device,
                     'epoch': 0,
                     'VADL': VADL}

        if args.local_rank == 0:
            run_model(args, arguments)

        return

    if args.statistics:
        arguments = {'model_1': model_1,
                     'model_2': model_2,
                     'device': device,
                     'epoch': 0,
                     'VADL': VADL}

        [count_errors, duration_errors, amplitude_errors, improper_measures] = compute_error_stats(args, arguments)
        if args.local_rank == 0:
            plot_stats(VADL, count_errors, duration_errors, amplitude_errors)
            print("This backbone produces {} improper measures.\nImproper measures are produced when the ground truth establishes 0 number of pulses but the network predicts one or more pulses."\
                    .format(improper_measures))

        return



























































def compute_error_stats(args, arguments):
    # switch to evaluate mode
    arguments['model_1'].eval()
    arguments['model_2'].eval()
    improper_measures = 0
    count_errors = torch.zeros(arguments['VADL'].shape)
    duration_errors = torch.zeros(arguments['VADL'].shape)
    amplitude_errors = torch.zeros(arguments['VADL'].shape)
    arguments['VADL'].reset_avail_winds(arguments['epoch'])
    for i in range(arguments['VADL'].total_number_of_windows):
        if i % args.world_size == args.local_rank:
            (Cnp, Duration, Dnp, window) = np.unravel_index(i, arguments['VADL'].shape)

            # bring a new window
            times, noisy_signals, clean_signals, _, labels = arguments['VADL'].get_signal_window(Cnp, Duration, Dnp, window)

            if labels[0] > 0:
                times = times.unsqueeze(0)
                noisy_signals = noisy_signals.unsqueeze(0)
                clean_signals = clean_signals.unsqueeze(0)
                labels = labels.unsqueeze(0)

                mean = torch.mean(noisy_signals, 1, True)
                noisy_signals = noisy_signals-mean

                with torch.no_grad():
                    noisy_signals = noisy_signals.unsqueeze(1)
                    num_of_pulses = arguments['model_1'](noisy_signals)
                    external = torch.reshape(num_of_pulses ,[1,1]).round()
                    outputs = arguments['model_2'](noisy_signals, external)
                    noisy_signals = noisy_signals.squeeze(1)

                    errors=abs((labels[:,1:].to('cpu') - outputs.data.to('cpu')*torch.Tensor([10**(-3), 10**(-10)]).repeat(1,1)) / labels[:,1:].to('cpu'))*100
                    errors=torch.mean(errors,dim=0)

                    duration_errors[Cnp, Duration, Dnp, window] = errors[0]
                    amplitude_errors[Cnp, Duration, Dnp, window] = errors[1]

                    error=abs((labels[:,0].to('cpu') - external.data.to('cpu')) / labels[:,0].to('cpu'))*100
                    error=torch.mean(error,dim=0)

                    count_errors[Cnp, Duration, Dnp, window] = error

            else:
                times = times.unsqueeze(0)
                noisy_signals = noisy_signals.unsqueeze(0)
                clean_signals = clean_signals.unsqueeze(0)
                labels = labels.unsqueeze(0)

                mean = torch.mean(noisy_signals, 1, True)
                noisy_signals = noisy_signals-mean

                with torch.no_grad():
                    noisy_signals = noisy_signals.unsqueeze(1)
                    num_of_pulses = arguments['model_1'](noisy_signals)
                    external = torch.reshape(num_of_pulses ,[1,1]).round()
                    noisy_signals = noisy_signals.squeeze(1)

                    if external.data.to('cpu') > 0.0:
                        count_errors[Cnp, Duration, Dnp, window] = torch.tensor(float('nan'))
                        duration_errors[Cnp, Duration, Dnp, window] = torch.tensor(float('nan'))
                        amplitude_errors[Cnp, Duration, Dnp, window] = torch.tensor(float('nan'))
                        improper_measures += 1
                    else:
                        count_errors[Cnp, Duration, Dnp, window] = 0.0
                        duration_errors[Cnp, Duration, Dnp, window] = 0.0
                        amplitude_errors[Cnp, Duration, Dnp, window] = 0.0

        #if args.test:
            #if i > 10:
                #break

    if args.distributed:
        reduced_count_error = Utilities.reduce_tensor_sum_dest(count_errors.data, 0)
        reduced_duration_error = Utilities.reduce_tensor_sum_dest(duration_errors.data, 0)
        reduced_amplitude_error = Utilities.reduce_tensor_sum_dest(amplitude_errors.data, 0)
    else:
        reduced_count_error = count_errors.data
        reduced_duration_error = duration_errors.data
        reduced_amplitude_error = amplitude_errors.data

    return [reduced_count_error, reduced_duration_error, reduced_amplitude_error, improper_measures]








def plot_stats(VADL, reduced_count_error, reduced_duration_error, reduced_amplitude_error):
    mean_count_error = reduced_count_error.numpy()
    mean_count_error = np.nanmean(mean_count_error, 3)

    std_count_error = reduced_count_error.numpy()
    std_count_error = np.nanstd(std_count_error, 3)

    mean_duration_error = reduced_duration_error.numpy()
    mean_duration_error = np.nanmean(mean_duration_error, 3)

    std_duration_error = reduced_duration_error.numpy()
    std_duration_error = np.nanstd(std_duration_error, 3)

    mean_amplitude_error = reduced_amplitude_error.numpy()
    mean_amplitude_error = np.nanmean(mean_amplitude_error, 3)

    std_amplitude_error = reduced_amplitude_error.numpy()
    std_amplitude_error = np.nanstd(std_amplitude_error, 3)

    (Cnp, Duration, Dnp) = VADL.shape[:3]

    ave0 = []
    std0 = []
    ave1 = []
    std1 = []
    ave2 = []
    std2 = []
    # setup the figure and axes for count errors
    fig = plt.figure(figsize=(10, 2*Duration*3.2))
    for i in range(Duration):
        ave0.append(fig.add_subplot(Duration,2,2*i+1, projection='3d'))
        std0.append(fig.add_subplot(Duration,2,2*i+2, projection='3d'))

    # prepare the data
    _x = np.arange(Cnp)
    _y = np.arange(Dnp)
    _xx, _yy = np.meshgrid(_x, _y)
    x, y = _xx.ravel(), _yy.ravel()
    width = depth = 1
    for i in range(Duration):
        top = mean_count_error[:,i,:].ravel()
        bottom = np.zeros_like(top)
        ave0[i].bar3d(x, y, bottom, width, depth, top, shade=True)
        ave0[i].set_title('Mean Count Error for Duration {}' .format(i+1))
        ave0[i].set_xlabel('Cnp')
        ave0[i].set_ylabel('Dnp')

        top = std_count_error[:,i,:].ravel()
        bottom = np.zeros_like(top)
        std0[i].bar3d(x, y, bottom, width, depth, top, shade=True, color='r')
        std0[i].set_title('STD Count Error for Duration {}' .format(i+1))
        std0[i].set_xlabel('Cnp')
        std0[i].set_ylabel('Dnp')

    plt.show()


    # setup the figure and axes for duration errors
    fig = plt.figure(figsize=(10, 2*Duration*3.2))
    for i in range(Duration):
        ave1.append(fig.add_subplot(Duration,2,2*i+1, projection='3d'))
        std1.append(fig.add_subplot(Duration,2,2*i+2, projection='3d'))

    # prepare the data
    _x = np.arange(Cnp)
    _y = np.arange(Dnp)
    _xx, _yy = np.meshgrid(_x, _y)
    x, y = _xx.ravel(), _yy.ravel()
    width = depth = 1
    for i in range(Duration):
        top = mean_duration_error[:,i,:].ravel()
        bottom = np.zeros_like(top)
        ave1[i].bar3d(x, y, bottom, width, depth, top, shade=True)
        ave1[i].set_title('Mean Duration Error for Duration {}' .format(i+1))
        ave1[i].set_xlabel('Cnp')
        ave1[i].set_ylabel('Dnp')

        top = std_duration_error[:,i,:].ravel()
        bottom = np.zeros_like(top)
        std1[i].bar3d(x, y, bottom, width, depth, top, shade=True, color='r')
        std1[i].set_title('STD Duration Error for Duration {}' .format(i+1))
        std1[i].set_xlabel('Cnp')
        std1[i].set_ylabel('Dnp')

    plt.show()


    # setup the figure and axes for amplitude errors
    fig = plt.figure(figsize=(10, 2*Duration*3.2))
    for i in range(Duration):
        ave2.append(fig.add_subplot(Duration,2,2*i+1, projection='3d'))
        std2.append(fig.add_subplot(Duration,2,2*i+2, projection='3d'))

    # prepare the data
    _x = np.arange(Cnp)
    _y = np.arange(Dnp)
    _xx, _yy = np.meshgrid(_x, _y)
    x, y = _xx.ravel(), _yy.ravel()
    width = depth = 1
    for i in range(Duration):
        top = mean_amplitude_error[:,i,:].ravel()
        bottom = np.zeros_like(top)
        ave2[i].bar3d(x+1, y+1, bottom, width, depth, top, shade=True)
        ave2[i].set_title('Mean Amplitude Error for Duration {}' .format(i+1))
        ave2[i].set_xlabel('Cnp')
        ave2[i].set_ylabel('Dnp')

        top = std_amplitude_error[:,i,:].ravel()
        bottom = np.zeros_like(top)
        std2[i].bar3d(x+1, y+1, bottom, width, depth, top, shade=True, color='r')
        std2[i].set_title('STD Amplitude Error for Duration {}' .format(i+1))
        std2[i].set_xlabel('Cnp')
        std2[i].set_ylabel('Dnp')

    plt.show()


    ave0 = []
    std0 = []
    ave1 = []
    std1 = []
    ave2 = []
    std2 = []
    count_error = reduced_count_error.numpy()
    duration_error = reduced_duration_error.numpy()
    amplitude_error = reduced_amplitude_error.numpy()
    for i in range(Duration):
        ave0.append(np.nanmean(count_error[:,i,:,:].ravel()))
        std0.append(np.nanstd(count_error[:,i,:,:].ravel()))
        ave1.append(np.nanmean(duration_error[:,i,:,:].ravel()))
        std1.append(np.nanstd(duration_error[:,i,:,:].ravel()))
        ave2.append(np.nanmean(amplitude_error[:,i,:,:].ravel()))
        std2.append(np.nanstd(amplitude_error[:,i,:,:].ravel()))


    fig, axs = plt.subplots(3, 2, figsize=(10,15))
    fig.tight_layout(pad=4.0)
    durations = [i for i in range(Duration)]

    axs[0,0].plot(durations,ave0)
    axs[0,0].set_title("Average count error: {}" .format(np.nanmean(count_error.ravel())))
    axs[0,0].set_xlabel("Duration")
    axs[0,0].set_ylabel("Average Error")

    axs[0,1].plot(durations,std0, color='r')
    axs[0,1].set_title("STD count error")
    axs[0,1].set_xlabel("Duration")
    axs[0,1].set_ylabel("STD Error")

    axs[1,0].plot(durations,ave1)
    axs[1,0].set_title("Average duration error: {}" .format(np.nanmean(duration_error.ravel())))
    axs[1,0].set_xlabel("Duration")
    axs[1,0].set_ylabel("Average Error")

    axs[1,1].plot(durations,std1, color='r')
    axs[1,1].set_title("STD duration error")
    axs[1,1].set_xlabel("Duration")
    axs[1,1].set_ylabel("STD Error")

    axs[2,0].plot(durations,ave2)
    axs[2,0].set_title("Average amplitude error: {}" .format(np.nanmean(amplitude_error.ravel())))
    axs[2,0].set_xlabel("Duration")
    axs[2,0].set_ylabel("Average Error")

    axs[2,1].plot(durations,std2, color='r')
    axs[2,1].set_title("STD amplitude error")
    axs[2,1].set_xlabel("Duration")
    axs[2,1].set_ylabel("STD Error")

    plt.show()























def run_model(args, arguments):
    # switch to evaluate mode
    arguments['model_1'].eval()
    arguments['model_2'].eval()

    arguments['VADL'].reset_avail_winds(arguments['epoch'])

    # bring a new batch
    times, noisy_signals, clean_signals, _, labels = arguments['VADL'].get_batch(descart_empty_windows=False)
    
    mean = torch.mean(noisy_signals, 1, True)
    noisy_signals = noisy_signals-mean

    with torch.no_grad():
        noisy_signals = noisy_signals.unsqueeze(1)
        num_of_pulses = arguments['model_1'](noisy_signals)
        zero_pulse_indices = torch.where(num_of_pulses.round()==0.0)[0].data
        external = torch.reshape(num_of_pulses ,[arguments['VADL'].batch_size,1]).round()
        outputs = arguments['model_2'](noisy_signals, external)
        noisy_signals = noisy_signals.squeeze(1)

    outputs[zero_pulse_indices,:] = 0.0

    times = times.cpu()
    noisy_signals = noisy_signals.cpu()
    clean_signals = clean_signals.cpu()
    labels = labels.cpu()


    if arguments['VADL'].batch_size < 21:
        fig, axs = plt.subplots(arguments['VADL'].batch_size, 1, figsize=(10,arguments['VADL'].batch_size*3))
        fig.tight_layout(pad=4.0)
        for i, batch_element in enumerate(range(arguments['VADL'].batch_size)):
            mean = torch.mean(noisy_signals[batch_element])
            axs[i].plot(times[batch_element],noisy_signals[batch_element]-mean)
            mean = torch.mean(clean_signals[batch_element])
            axs[i].plot(times[batch_element],clean_signals[batch_element]-mean)
            axs[i].set_title("Average translocation time: {}, prediction is {}\nAverage aplitude: {}, prediction is {}\nNumber of pulses is {}, prediction is {}."
            .format(labels[batch_element,1], outputs[batch_element,0]*10**(-3),\
                        labels[batch_element,2], outputs[batch_element,1]*10**(-10),\
                        round(labels[batch_element,0].item()), round(num_of_pulses[batch_element,0].item())))
    else:
        print('This will not show more than 20 plots')

    plt.show()


    count_error = 0.0
    duration_error = 0.0
    amplitude_error = 0.0
    measures = 0.0
    improper_measures = 0.0
    for i in range(arguments['VADL'].batch_size):
        if labels[i,0] == 0.0:
            if (i == zero_pulse_indices).any():
                measures += 1.0
            else:
                improper_measures += 1.0
        else:
            measures += 1.0
            count_error += abs((labels[i,0] - external[i,0].data.to('cpu')) / labels[i,0])*100
            duration_error += abs((labels[i,1] - outputs[i,0].data.to('cpu')*10**(-3)) / labels[i,1])*100
            amplitude_error += abs((labels[i,2] - outputs[i,1].data.to('cpu')*10**(-10)) / labels[i,2])*100


    print("Average translocation duration error: {0:.1f}%\nAverage translocation amplitude error: {1:.1f}%\nAverage translocation counter error: {2:.1f}%"\
            .format(duration_error.item()/measures, amplitude_error.item()/measures, count_error.item()/measures))

    print("In this batch we has {} improper measures.\nImproper measures are produced when the ground truth establishes 0 number of pulses but the network predicts one or more pulses."\
            .format(int(improper_measures)))
















if __name__ == '__main__':
    main()