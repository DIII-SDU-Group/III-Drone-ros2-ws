#!/usr/bin/python3

import argparse

import os
import re

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
        '-p',
        type=str, 
        help='The name of the ROS2 packages to validate.',
        nargs='+',
    )
    
    parser.add_argument(
        '--headers', 
        type=str, 
        help='The name of the C++ headers to validate.',
        nargs='+',
    )
    
    parser.add_argument(
        '--sources', 
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
        parser.error('At least one of --ros2-packages, --headers, or --sources must be supplied.')
        
    # Check that either packages or header/sources are specified, but not both
    if args.ros2_packages is not None and (args.headers is not None or args.sources is not None):
        parser.error('Cannot specify both --ros2-packages and --headers or --sources.')
    
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

def get_cpp_files_from_ros2_package(parser, ros2_package):
    script_dir = os.path.dirname(os.path.realpath(__file__))

    headers = []
    sources = []
    
    package_dir = find_ros2_package_directory(parser, ros2_package)
    
    for root, dirs, files in os.walk(package_dir):
        for file in files:
            if file.endswith('.hpp') or file.endswith('.h'):
                headers.append(os.path.join(root, file))
            elif file.endswith('.cpp'):
                sources.append(os.path.join(root, file))
    
    return headers, sources

def has_parameter_getter(file):
    with open(file, 'r') as f:
        content = f.read()
        matches = re.findall(r'(\w+)\.GetParameter\("([^"]+)"\)\.as_\w+\(\)', content)
        if matches:
            return matches[0][0]
        else:
            return None

def get_sources_and_headers_with_parameters(sources, headers):
    headers_with_params = []
    sources_with_params = []
    
    for header in headers:
        if parameter_object := has_parameter_getter(header):
            headers_with_params.append((header, parameter_object))
            
    for source in sources:
        if parameter_object := has_parameter_getter(source):
            sources_with_params.append((source, parameter_object))
            
    return headers_with_params, sources_with_params

def find_matching_header(source, headers):
    with open(source, 'r') as f:
        content = f.read()
        # matches = re.findall(r'#include', content)
        matches = re.findall(r'#include\s+["|<]([^"]+)["|>]', content)
        for match in matches:
            for header in headers:
                if match in header:
                    return header
    
    return None

def find_parameter_access_entries(
    headers, 
    sources, 
    headers_with_params, 
    sources_with_params
):
    parameter_access_entries = []
    
    for header, parameter_object in headers_with_params:
        entry = {"source": None, "header": header, "parameter_object": parameter_object}
        
        parameter_object_instantiation = get_parameter_object_instantiation(header, parameter_object)
        
        parameter_access_entries.append(entry)
        
    for source, parameter_object in sources_with_params:
        header = find_matching_header(source, headers)
        entry = {"source": source, "header": header}
        parameter_access_entries.append(entry)

    
    
    return parameter_access_entries

def main():
    parser, args = get_args()
    args = validate_args(parser, args)
    
    headers, sources = [], []
    
    if args.ros2_packages is not None:
        for ros2_package in args.ros2_packages:
            package_headers, package_sources = get_cpp_files_from_ros2_package(parser, ros2_package)
            headers.extend(package_headers)
            sources.extend(package_sources)
            
        headers_with_params, sources_with_params = get_sources_and_headers_with_parameters(sources, headers)

        i = 2
        print(sources[i])
        matching_header = find_matching_header(sources[i], headers)

        print(matching_header)
        
        # parameter_access_entries = find_parameter_access_entries(
        #     headers,
        #     sources,
        #     headers_with_params, 
        #     sources_with_params
        # )
            
    
    
if __name__ == '__main__':
    main()

