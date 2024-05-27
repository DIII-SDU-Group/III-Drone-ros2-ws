#!/usr/bin/python3

import argparse

import os

import xml.etree.ElementTree as ET

def get_args():
    parser = argparse.ArgumentParser(description='Helper script to validate configuration during development. CONFIG_BASE_DIR must be supplied as an environment variable or as an argument to the script. Currently only works for C++ nodes.')
    
    parser.add_argument(
        '--config-base-dir', 
        '-c', 
        type=str, 
        help='The base directory of the configuration files. If not supplied, the script will look for the CONFIG_BASE_DIR environment variable.'
    )
    
    # Arguments to select targets:
    # ROS2 package name(s), or
    # C++ header and/or source file(s):
    parser.add_argument(
        '--ros2-packages', 
        type=str, 
        help='The name of the ROS2 packages to validate.',
        nargs='+',
    )
    
    parser.add_argument(
        '--cpp-headers', 
        type=str, 
        help='The name of the C++ headers to validate.',
        nargs='+',
    )
    
    parser.add_argument(
        '--cpp-sources', 
        type=str, 
        help='The name of the C++ sources to validate.',
        nargs='+',
    )

    args = parser.parse_args()
    
    return parser, args

def validate_args(parser, args):
    if args.config_base_dir is None:
        if 'CONFIG_BASE_DIR' in os.environ:
            args.config_base_dir = os.environ['CONFIG_BASE_DIR']
        else:
            parser.error('CONFIG_BASE_DIR must be supplied as an environment variable or as an argument to the script.')
    
    if args.ros2_packages is None and args.cpp_headers is None and args.cpp_sources is None:
        parser.error('At least one of --ros2-packages, --cpp-headers, or --cpp-sources must be supplied.')
    
    return args

def get_all_package_names(parser):
    script_dir = os.path.dirname(os.path.realpath(__file__))
    src_dir = os.path.abspath(os.path.join(script_dir, '..', 'src'))
    
    package_names = []
    
    for root, dirs, files in os.walk(src_dir):
        if 'package.xml' in files:
            tree = ET.parse(os.path.join(root, 'package.xml'))
            root_element = tree.getroot()
            package_name_element = root_element.find('name')
            if package_name_element is not None:
                package_names.append(package_name_element.text)
    
    return package_names

def find_ros2_package_directory(parser, ros2_package):
    script_dir = os.path.dirname(os.path.realpath(__file__))
    src_dir = os.path.abspath(os.path.join(script_dir, '..', 'src'))
    
    for root, dirs, files in os.walk(src_dir):
        if 'package.xml' in files:
            tree = ET.parse(os.path.join(root, 'package.xml'))
            root_element = tree.getroot()
            package_name_element = root_element.find('name')
            if package_name_element is not None and package_name_element.text == ros2_package:
                return root
    
    error_string = 'Could not find ROS2 package directory for package: \'' + ros2_package + "\'. Package must be one of the following: \n"
    for package_name in get_all_package_names(parser):
        error_string += "\t'" + package_name + "'\n"

    parser.error(error_string)

def get_cpp_files_from_ros2_package(ros2_package):
    script_dir = os.path.dirname(os.path.realpath(__file__))
    
    

def main():
    parser, args = get_args()
    args = validate_args(parser, args)
    
    find_ros2_package_directory(parser, args.ros2_packages[0])
    
if __name__ == '__main__':
    main()

