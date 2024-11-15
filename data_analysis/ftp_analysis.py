#!/usr/bin/python3

import argparse
import rosbag2_py
from rclpy.serialization import deserialize_message
import os
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
import numpy as np

from tf2_msgs.msg import TFMessage
from nav_msgs.msg import Path
from geometry_msgs.msg import PoseStamped
from px4_msgs.msg import VehicleOdometry
from iii_drone_interfaces.msg import StringStamped, Target

from iii_drone_core.utils import math

TOPIC_TYPE_MAP = {
    '/tf': TFMessage,
    '/control/trajectory_generator/trajectory_path': Path,
    '/control/trajectory_generator/target_pose': PoseStamped,
    '/fmu/out/vehicle_odometry': VehicleOdometry,
    '/mission/mission_executor/maneuver_reference_client/reference_mode': StringStamped
}

class FTPData:
    def __init__(
        self,
        tf_world_to_drone: "list[TFMessage]",
        traj_path: "list[Path]",
        tg_target_pose: "list[PoseStamped]",
        vehicle_odometry: "list[VehicleOdometry]",
        reference_mode: "list[StringStamped]",
    ):
        self.tf_world_to_drone: "list[TFMessage]" = tf_world_to_drone
        self.traj_path: "list[Path]" = traj_path
        self.tg_target_pose: "list[PoseStamped]" = tg_target_pose
        self.vehicle_odometry: "list[VehicleOdometry]" = vehicle_odometry
        self.reference_mode: "list[StringStamped]" = reference_mode
        
        self.tf_world_to_drone_np, self.tf_ori_np, self.tf_world_to_drone_t = self._tf_to_numpy(self.tf_world_to_drone)
        self.traj_path_np = self._path_to_numpy(self.traj_path)
        self.tg_target_pos_np, self.tg_target_pose_t = self._pose_to_numpy(self.tg_target_pose)
        self.vehicle_odometry_np, self.vehicle_odometry_t = self._vehicle_odometry_to_numpy(self.vehicle_odometry)

        if not self._check_target_pose(self.tg_target_pos_np):
            raise Exception("Target Pose is not constant")
        
    def _check_target_pose(self, target_pose_np: np.array) -> bool:
        for i in range(1, target_pose_np.shape[0]):
            if not np.allclose(target_pose_np[i], target_pose_np[i-1]):
                return False
            
        return True

    def _tf_to_numpy(self, tf: "list[TFMessage]") -> "tuple[np.array,np.array]":
        arr = []
        ori = []
        t = []
        
        for tf_msg in tf:
            arr.append([
                tf_msg.transforms[0].transform.translation.x,
                tf_msg.transforms[0].transform.translation.y,
                tf_msg.transforms[0].transform.translation.z
            ])
            ori.append([
                tf_msg.transforms[0].transform.rotation.w,
                tf_msg.transforms[0].transform.rotation.x,
                tf_msg.transforms[0].transform.rotation.y,
                tf_msg.transforms[0].transform.rotation.z
            ])
            t.append(tf_msg.transforms[0].header.stamp.sec + tf_msg.transforms[0].header.stamp.nanosec * 1e-9)
            
        return np.array(arr), np.array(ori), np.array(t)
    
    def _path_to_numpy(self, path: "list[Path]") -> "tuple[np.array,np.array]":
        arr = []
        t = []
        
        for path_msg in path:
            arr.append([
                path_msg.poses[0].pose.position.x,
                path_msg.poses[0].pose.position.y,
                path_msg.poses[0].pose.position.z
            ])
            t.append(path_msg.header.stamp.sec + path_msg.header.stamp.nanosec * 1e-9)
                
        return np.array(arr)
    
    def _pose_to_numpy(self, pose: "list[PoseStamped]") -> "tuple[np.array,np.array]":
        arr = []
        t = []
        
        for pose_msg in pose:
            arr.append([
                pose_msg.pose.position.x,
                pose_msg.pose.position.y,
                pose_msg.pose.position.z
            ])
            t.append(pose_msg.header.stamp.sec + pose_msg.header.stamp.nanosec * 1e-9)
            
        return np.array(arr), np.array(t)
    
    def _vehicle_odometry_to_numpy(self, vehicle_odometry: "list[VehicleOdometry]") -> "tuple[np.array,np.array]":
        arr = []
        t = []
        
        for odom_msg in vehicle_odometry:
            arr.append([
                odom_msg.position[0],
                odom_msg.position[1],
                odom_msg.position[2]
            ])
            # Timestamp in microseconds
            t.append(odom_msg.timestamp * 1e-6)
            
        return np.array(arr), np.array(t)

    def normalize(self, target_coordinates: np.array) -> "FTPData":
        tf_ori = self.tf_ori_np[-1]

        rot = math.quatToMat(tf_ori)

        self.tf_world_to_drone_np = (rot.T @ self.tf_world_to_drone_np.T).T
        self.tg_target_pos_np = (rot.T @ self.tg_target_pos_np.T).T
        self.traj_path_np = (rot.T @ self.traj_path_np.T).T

        target_pos = self.tg_target_pos_np[0]
        
        diff = target_pos - target_coordinates
        
        self.tf_world_to_drone_np = self.tf_world_to_drone_np - diff
        self.tg_target_pos_np = self.tg_target_pos_np - diff
        self.traj_path_np = self.traj_path_np - diff
        
        # Multiply the inverse rotation matrix with all elements and subtract the difference
        # to get the coordinates in the target frame
        
        # Apply rot and diff to all the coordinates
        # self.tf_world_to_drone_np = 
        # self.traj_path_np = self.traj_path_np - diff
        # self.vehicle_odometry_np = self.vehicle_odometry_np - diff

        first_time = self.tf_world_to_drone_t[0]

        if self.tg_target_pose_t[0] < first_time:
            first_time = self.tg_target_pose_t[0]
            
        if self.vehicle_odometry_t[0] < first_time:
            first_time = self.vehicle_odometry_t[0]
            
        self.tf_world_to_drone_t = self.tf_world_to_drone_t - first_time
        # self.traj_path_t = self.traj_path_t - first_time
        self.tg_target_pose_t = self.tg_target_pose_t - first_time
        self.vehicle_odometry_t = self.vehicle_odometry_t - first_time
        
        return self

def get_readers(dir: str) -> "list[rosbag2_py.SequentialReader]":
    bag_dirs = os.listdir(dir)
    # Sort the bag directories
    bag_dirs.sort()
    
    readers = []
    
    for bag_dir in bag_dirs:
        # Inside bag_dir, find the file with the .db3 extension
        bag_dir = os.path.join(dir, bag_dir)
        
        reader = rosbag2_py.SequentialReader()
        
        storage_options = rosbag2_py._storage.StorageOptions(
            uri=bag_dir
        )
        
        converter_options = rosbag2_py._storage.ConverterOptions('','')
        
        reader.open(storage_options, converter_options)
        
        readers.append(reader)
        
    return readers

def get_data(reader: rosbag2_py.SequentialReader) -> FTPData:
    tf_world_to_drone: "list[TFMessage]" = []
    traj_path: "list[Path]" = []
    tg_target_pose: "list[PoseStamped]" = []
    vehicle_odometry: "list[VehicleOdometry]" = []
    reference_mode: "list[StringStamped]" = []
    
    topic_list_map = {
        '/tf': tf_world_to_drone,
        '/control/trajectory_generator/trajectory_path': traj_path,
        '/control/trajectory_generator/target_pose': tg_target_pose,
        '/fmu/out/vehicle_odometry': vehicle_odometry,
        '/mission/mission_executor/maneuver_reference_client/reference_mode': reference_mode,
    }

    latest_tf = None
    latest_tp = None
    latest_odom = None
    latest_ref_mode = None
    
    traj_started = False
    
    while reader.has_next():
        topic, msg, t = reader.read_next()
        
        if topic in topic_list_map:
            msg_deser = deserialize_message(msg, TOPIC_TYPE_MAP[topic])
            
            if traj_started:
                if topic == "/mission/mission_executor/maneuver_reference_client/reference_mode":
                    if msg_deser.data == "hover":
                        break

                if topic == "/tf" and not (msg_deser.transforms[0].child_frame_id == "drone" and msg_deser.transforms[0].header.frame_id == "world"):
                    continue
                    
                topic_list_map[topic].append(msg_deser)
                
            else:
                if topic == "/control/trajectory_generator/trajectory_path":
                    traj_started = True
                    topic_list_map[topic].append(msg_deser)
                    
                    if latest_tf is not None:
                        tf_world_to_drone.append(latest_tf)
                        
                    if latest_tp is not None:
                        tg_target_pose.append(latest_tp)
                        
                    if latest_odom is not None:
                        vehicle_odometry.append(latest_odom)
                        
                    if latest_ref_mode is not None:
                        reference_mode.append(latest_ref_mode)
                    
                elif topic == "/tf":
                    if not (msg_deser.transforms[0].child_frame_id == "drone" and msg_deser.transforms[0].header.frame_id == "world"):
                        continue
                    latest_tf = msg_deser
                    
                elif topic == "/control/trajectory_generator/target_pose":
                    latest_tp = msg_deser
                    
                elif topic == "/fmu/out/vehicle_odometry":
                    latest_odom = msg_deser
                    
                elif topic == "/mission/mission_executor/maneuver_reference_client/reference_mode":
                    latest_ref_mode = msg_deser
                    
    print(f"FTP Data: {len(tf_world_to_drone)} {len(traj_path)} {len(tg_target_pose)} {len(vehicle_odometry)} {len(reference_mode)}")
            
    return FTPData(
        tf_world_to_drone,
        traj_path,
        tg_target_pose,
        vehicle_odometry,
        reference_mode
    ).normalize(np.array([3,3,3]))

def get_ftp_objects(readers: "list[rosbag2_py.SequentialReader]") -> "list[FTPData]":
    ftp_objects = []
    
    for reader in readers:
        ftp_objects.append(get_data(reader))
        
    return ftp_objects

def make_plot(
    ftp_objects: "list[FTPData]", 
    title: str,
    skip_paths: bool = False,
    run_offset: int = 0
) -> "tuple[plt.Figure,plt.Axes]":
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')
    
    straight_line = np.linspace([0,0,0], [3,3,3], 100)
    
    ax.plot(
        straight_line[:,0],
        straight_line[:,1],
        straight_line[:,2],
        label="Straight Path",
        color='black',
        linestyle='dashed'
    )
        
    # Create a black X at the target position
    ax.scatter(
        ftp_objects[0].tg_target_pos_np[0,0],
        ftp_objects[0].tg_target_pos_np[0,1],
        ftp_objects[0].tg_target_pos_np[0,2],
        color='red',
        marker='x',
        label="Target Position"
    )
    
    # Main colors: blue, orange, green
    main_colors = ['blue', 'orange', 'green']
    
    # Path colors: light blue, light orange, light green
    path_colors = ['lightblue', 'lightcoral', 'lightgreen']
    
    if not skip_paths:
        for i, ftp in enumerate(ftp_objects):
            ax.plot(
                ftp.traj_path_np[:,0],
                ftp.traj_path_np[:,1],
                ftp.traj_path_np[:,2],
                linestyle='dashed',
                # Smaller line width for the trajectory
                linewidth=1,
                color=main_colors[i]
            )
            # for j in range(0,ftp.traj_path_np.shape[0],5):
            #     ax.plot(
            #         ftp.traj_path_np[j][:,0],
            #         ftp.traj_path_np[j][:,1],
            #         ftp.traj_path_np[j][:,2],
            #         linestyle='dotted',
            #         # Smaller line width for the trajectory
            #         linewidth=1,
            #         color=main_colors[i]
            #     )

    for i, ftp in enumerate(ftp_objects):
        ax.plot(
            ftp.tf_world_to_drone_np[:,0],
            ftp.tf_world_to_drone_np[:,1],
            ftp.tf_world_to_drone_np[:,2],
            label="Run " + str(i+1+run_offset),
            color=main_colors[i],
            linewidth=2
        )
        
    # ax.set_title(title)

    ax.legend()
    
    # Increase font size of the legend
    for text in ax.get_legend().get_texts():
        text.set_fontsize('large')
        
    # Increase font size of the axis numbers
    for tick in ax.xaxis.get_major_ticks():
        tick.label.set_fontsize('large')
    

    plt.show()
    
    return fig, ax

def main():
    parser = argparse.ArgumentParser(description='FTP Analysis')
    parser.add_argument(
        '--open-loop-mpc-dir', 
        type=str, 
        help='Open Loop MPC Rosbags Directory',
        required=True
    )
    parser.add_argument(
        '--closed-loop-mpc-dir', 
        type=str, 
        help='Closed Loop MPC Rosbags Directory',
        required=True
    )
    parser.add_argument(
        '--quintic-interpolation-dir', 
        type=str, 
        help='Quintic Interpolation Rosbags Directory',
        required=True
    )
    parser.add_argument(
        '--results-output-dir', 
        type=str, 
        help='Results Output Directory',
        required=True
    )
    args = parser.parse_args()
    
    # Check if the output directory exists, if it does, check if it is empty
    if not os.path.exists(args.results_output_dir):
        os.makedirs(args.results_output_dir)
    
    open_loop_mpc_readers = get_readers(args.open_loop_mpc_dir)
    closed_loop_mpc_readers = get_readers(args.closed_loop_mpc_dir)
    quintic_interpolation_readers = get_readers(args.quintic_interpolation_dir)

    open_loop_mpc_ftps = get_ftp_objects(open_loop_mpc_readers)
    closed_loop_mpc_ftps = get_ftp_objects(closed_loop_mpc_readers)
    quintic_interpolation_ftps = get_ftp_objects(quintic_interpolation_readers)
    
    figs = [
        make_plot(open_loop_mpc_ftps, "Open Loop MPC"),
        make_plot(closed_loop_mpc_ftps, "Closed Loop MPC", run_offset=len(open_loop_mpc_ftps)),
        make_plot(quintic_interpolation_ftps, "Quintic Interpolation", skip_paths=True, run_offset=len(open_loop_mpc_ftps) + len(closed_loop_mpc_ftps))
    ]
    
if __name__ == '__main__':
    main()