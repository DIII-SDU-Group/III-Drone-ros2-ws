#!/usr/bin/env python3
"""Create an exact sensor/truth-aligned MCAP from a raw qualification bag."""
from __future__ import annotations
import argparse
from pathlib import Path
import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message

GROUPS=(
 ('/sensor/cable_camera/image_raw','/simulation/ground_truth/cable_camera/conductor_instance_mask','/simulation/ground_truth/cable_camera/frame'),
 ('/sensor/mmwave/points_full','/simulation/ground_truth/mmwave/scan'),
)
def stamp(data,cls):
 m=deserialize_message(data,cls);return int(m.header.stamp.sec)*1_000_000_000+int(m.header.stamp.nanosec)
def align(source:Path,dest:Path):
 r=rosbag2_py.SequentialReader();r.open(rosbag2_py.StorageOptions(uri=str(source),storage_id=''),rosbag2_py.ConverterOptions('',''))
 metas=r.get_all_topics_and_types(); types={m.name:get_message(m.type) for m in metas}; wanted={x for g in GROUPS for x in g}; stamps={x:set() for x in wanted}
 while r.has_next():
  topic,data,_=r.read_next()
  if topic in wanted: stamps[topic].add(stamp(data,types[topic]))
 valid={}
 for group in GROUPS:
  common=set.intersection(*(stamps[x] for x in group))
  for topic in group: valid[topic]=common
 r=rosbag2_py.SequentialReader();r.open(rosbag2_py.StorageOptions(uri=str(source),storage_id=''),rosbag2_py.ConverterOptions('',''))
 w=rosbag2_py.SequentialWriter();w.open(rosbag2_py.StorageOptions(uri=str(dest),storage_id='mcap'),rosbag2_py.ConverterOptions('',''))
 for meta in metas:w.create_topic(meta)
 kept={x:0 for x in wanted};dropped={x:0 for x in wanted}
 while r.has_next():
  topic,data,receipt=r.read_next()
  if topic in wanted:
   if stamp(data,types[topic]) not in valid[topic]: dropped[topic]+=1;continue
   kept[topic]+=1
  w.write(topic,data,receipt)
 return {'kept':kept,'dropped_unmatched_boundary_messages':dropped}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('source',type=Path);ap.add_argument('dest',type=Path);a=ap.parse_args();print(align(a.source,a.dest))
if __name__=='__main__':main()
