#!/usr/bin/python3

import argparse
import rosbag2_py
from rclpy.serialization import deserialize_message
import os

from iii_drone_interfaces.msg import TrajectoryComputeTime

def get_mpc_compute_times(bag_dir):
    reader = rosbag2_py.SequentialReader()
    storage_options = rosbag2_py._storage.StorageOptions(uri=bag_dir)
    converter_options = rosbag2_py._storage.ConverterOptions('','')
    
    reader.open(storage_options, converter_options)
    
    compute_times = []
    
    while reader.has_next():
        topic, msg, t = reader.read_next()
        if topic == '/control/trajectory_generator/trajectory_compute_time':
            msg = deserialize_message(msg, TrajectoryComputeTime)
            
            # Convert to seconds from nanoseconds
            compute_times.append(msg.nanoseconds / 1e9)
    
    return compute_times

def main():
    parser = argparse.ArgumentParser(description='Analyze MPC compute time')
    parser.add_argument('bag_dirs', type=str, help='Rosbag directories', nargs='+')
    args = parser.parse_args()
    
    compute_times = []

    for bag_dir in args.bag_dirs:
        compute_times.extend(get_mpc_compute_times(bag_dir))
        
    # Print avg, min, max
    print('Avg MPC compute time: {:.10f} s'.format(sum(compute_times) / len(compute_times)))
    print('Min MPC compute time: {:.10f} s'.format(min(compute_times)))
    print('Max MPC compute time: {:.10f} s'.format(max(compute_times)))
        
if __name__ == '__main__':
    main()