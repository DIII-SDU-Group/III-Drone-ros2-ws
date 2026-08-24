#!/usr/bin/env python3
"""Standalone PX4 offboard executor for one qualification flight."""
from __future__ import annotations
import argparse,csv,json,math,os,signal,subprocess,sys,time
from pathlib import Path
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile,QoSReliabilityPolicy,QoSHistoryPolicy,QoSDurabilityPolicy
from px4_msgs.msg import OffboardControlMode,TrajectorySetpoint,VehicleCommand,VehicleLocalPosition,VehicleStatus,FailsafeFlags,HealthReport,VehicleCommandAck
from iii_drone_interfaces.msg import SimulatorDroneState,PLMapperCommand
from iii_drone_interfaces.srv import PLMapperCommand as PLMapperCommandService

sys.path.insert(0,str(Path(__file__).resolve().parent))
import powerline_qualification as catalog
from synchronize_powerline_ground_truth_bag import align as align_bag


def qos():
    return QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT,durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,history=QoSHistoryPolicy.KEEP_LAST,depth=1)


class Executor(Node):
    def __init__(self,g):
        super().__init__('powerline_qualification_offboard_executor')
        self.g=g; self.local=None; self.status=None; self.gt=None; self.failsafe=None; self.health=None; self.ack=None; self.offset=None; self.samples=[]
        self.sp=self.create_publisher(TrajectorySetpoint,'/fmu/in/trajectory_setpoint',qos())
        self.mode=self.create_publisher(OffboardControlMode,'/fmu/in/offboard_control_mode',qos())
        self.command=self.create_publisher(VehicleCommand,'/fmu/in/vehicle_command',qos())
        self.create_subscription(VehicleLocalPosition,'/fmu/out/vehicle_local_position',self._local,qos())
        self.create_subscription(VehicleStatus,'/fmu/out/vehicle_status_v1',self._status,qos())
        self.create_subscription(FailsafeFlags,'/fmu/out/failsafe_flags',lambda m:setattr(self,'failsafe',m),qos())
        self.create_subscription(HealthReport,'/fmu/out/health_report',lambda m:setattr(self,'health',m),qos())
        self.create_subscription(VehicleCommandAck,'/fmu/out/vehicle_command_ack',lambda m:setattr(self,'ack',m),qos())
        self.create_subscription(SimulatorDroneState,'/simulation/ground_truth/drone/state',self._gt,10)
        self.mapper=self.create_client(PLMapperCommandService,'/perception/pl_mapper/pl_mapper_command')
    def _local(self,m): self.local=m
    def _status(self,m): self.status=m
    def _gt(self,m): self.gt=m
    def stamp(self): return int(self.get_clock().now().nanoseconds/1000)
    @staticmethod
    def world_to_ned_vec(v): return np.array([v[1],v[0],-v[2]],float)
    def calibrate(self):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and (self.local is None or self.gt is None): rclpy.spin_once(self,timeout_sec=.1)
        if self.local is None or self.gt is None: raise RuntimeError('local position or independent simulator GT unavailable')
        p=self.gt.pose_world.position; world=np.array([p.x,p.y,p.z]); local=np.array([self.local.x,self.local.y,self.local.z])
        self.offset=local-self.world_to_ned_vec(world)
    def pG_to_local(self,p): return self.world_to_ned_vec(self.g.world(p[0],p[1],p[2])[0])+self.offset
    def vG_to_local(self,v):
        world=np.array([*(v[0]*self.g.x_axis+v[1]*self.g.y_axis),v[2]])
        return self.world_to_ned_vec(world)
    def yaw_local(self,yawG):
        world_yaw=math.atan2(self.g.x_axis[1],self.g.x_axis[0])+yawG
        return math.atan2(math.sin(math.pi/2-world_yaw),math.cos(math.pi/2-world_yaw))
    def publish(self,pG,vG,yaw,yawrate,aG=None):
        now=self.stamp(); m=OffboardControlMode(); m.timestamp=now; m.position=True; m.velocity=True; self.mode.publish(m)
        s=TrajectorySetpoint(); s.timestamp=now; s.position=self.pG_to_local(pG).astype(np.float32).tolist(); s.velocity=self.vG_to_local(vG).astype(np.float32).tolist(); s.acceleration=([math.nan]*3 if aG is None else self.vG_to_local(aG).astype(np.float32).tolist()); s.jerk=[math.nan]*3; s.yaw=float(self.yaw_local(yaw)); s.yawspeed=float(-yawrate); self.sp.publish(s)
    def vehicle_command(self,cmd,p1=0.,p2=0.,external=True):
        m=VehicleCommand(); m.timestamp=self.stamp(); m.command=cmd; m.param1=float(p1); m.param2=float(p2); m.target_system=int(os.getenv('PX4_TARGET_SYSTEM','1')); m.target_component=1; m.source_system=1; m.source_component=1; m.from_external=external; self.command.publish(m)
    def stream(self,p,v,yaw,rate,duration,collect=False,t0=0.):
        period=1/catalog.SETPOINT_HZ; start=time.monotonic(); next_tick=start
        while time.monotonic()-start<duration:
            self.publish(p,v,yaw,rate); rclpy.spin_once(self,timeout_sec=0)
            if collect and self.gt:
                q=self.gt.pose_world.orientation; pos=self.gt.pose_world.position; tw=self.gt.twist_world
                w=np.array([pos.x,pos.y,pos.z]); dg=w[:2]-self.g.origin_xy; actual=np.array([dg@self.g.x_axis,dg@self.g.y_axis,w[2]])
                siny=2*(q.w*q.z+q.x*q.y); cosy=1-2*(q.y*q.y+q.z*q.z); yw=math.atan2(siny,cosy); yg=math.atan2(math.sin(yw-math.atan2(self.g.x_axis[1],self.g.x_axis[0])),math.cos(yw-math.atan2(self.g.x_axis[1],self.g.x_axis[0])))
                vw=np.array([tw.linear.x,tw.linear.y,tw.linear.z]); av=np.array([vw[:2]@self.g.x_axis,vw[:2]@self.g.y_axis,vw[2]])
                self.samples.append([t0+time.monotonic()-start,*p,*v,yaw,rate,*actual,*av,yg,int(self.status.nav_state) if self.status else -1])
            next_tick+=period; time.sleep(max(0,next_tick-time.monotonic()))
    def enter_offboard_and_arm(self,p):
        for _ in range(60): self.publish(p,np.zeros(3),0,0); rclpy.spin_once(self,timeout_sec=.01); time.sleep(.04)
        for _ in range(5): self.vehicle_command(VehicleCommand.VEHICLE_CMD_DO_SET_MODE,1,6); time.sleep(.1)
        for _ in range(5): self.vehicle_command(VehicleCommand.VEHICLE_CMD_COMPONENT_ARM_DISARM,1); time.sleep(.1)
        deadline=time.monotonic()+8
        forced=False
        while time.monotonic()<deadline:
            self.publish(p,np.zeros(3),0,0); rclpy.spin_once(self,timeout_sec=.02)
            if self.status and self.status.nav_state==VehicleStatus.NAVIGATION_STATE_OFFBOARD and self.status.arming_state==VehicleStatus.ARMING_STATE_ARMED:return
            time.sleep(.02)
        # Isolated SITL has no GCS/data-link and can retain a false aggregate
        # preflight flag even with valid local position. PX4's documented
        # magic value explicitly force-arms; all executor safety gates remain.
        self.vehicle_command(VehicleCommand.VEHICLE_CMD_COMPONENT_ARM_DISARM,1,21196,external=False)
        forced=True; deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            self.publish(p,np.zeros(3),0,0); rclpy.spin_once(self,timeout_sec=.02)
            if self.status and self.status.nav_state==VehicleStatus.NAVIGATION_STATE_OFFBOARD and self.status.arming_state==VehicleStatus.ARMING_STATE_ARMED:return
            time.sleep(.02)
        raise RuntimeError(f'failed to enter armed offboard: status={self.status}; failsafe={self.failsafe}; health={self.health}; ack={self.ack}')
    def mapper_start(self,p,yaw):
        deadline=time.monotonic()+10
        while not self.mapper.service_is_ready() and time.monotonic()<deadline:
            self.publish(p,np.zeros(3),yaw,0); rclpy.spin_once(self,timeout_sec=.02)
        if not self.mapper.service_is_ready(): raise RuntimeError('PL mapper service unavailable')
        req=PLMapperCommandService.Request(); req.pl_mapper_cmd.command=PLMapperCommand.PL_MAPPER_CMD_START; req.pl_mapper_cmd.reset=True
        future=self.mapper.call_async(req); deadline=time.monotonic()+10
        while not future.done() and time.monotonic()<deadline:
            self.publish(p,np.zeros(3),yaw,0); rclpy.spin_once(self,timeout_sec=.02)
        if not future.done() or future.result().pl_mapper_ack!=PLMapperCommandService.Response.PL_MAPPER_ACK_SUCCESS: raise RuntimeError('PL mapper start failed')


def record_command(path):
    return ['ros2','bag','record','--storage','mcap','--max-cache-size','1073741824','-o',str(path),*catalog.RECORD_TOPICS]


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('flight_id'); ap.add_argument('--output-root',type=Path,default=catalog.OUTPUT); ap.add_argument('--smoke',action='store_true'); a=ap.parse_args()
    spec=next(x for x in catalog.ALL_FLIGHTS if x['flight_id']==a.flight_id); g=catalog.Corridor.load(); tr=catalog.build_flight(spec,g)
    report=catalog.validate(tr,g)
    if report['status']!='passed': raise SystemExit('preflight validation did not pass')
    out=a.output_root/spec['split']/a.flight_id; bag=out/'bag'; raw_bag=out/'bag_raw'
    for candidate in (bag,raw_bag):
        if candidate.exists() and any(candidate.iterdir()): raise SystemExit(f'refusing to overwrite nonempty bag {candidate}')
        if candidate.exists(): candidate.rmdir()
    rclpy.init(); node=Executor(g); recorder=None
    try:
        node.calibrate(); start=tr.p[0]; current_gt=node.gt.pose_world.position; w=np.array([current_gt.x,current_gt.y,current_gt.z]); dg=w[:2]-g.origin_xy; current=np.array([dg@g.x_axis,dg@g.y_axis,w[2]])
        node.enter_offboard_and_arm(current)
        # Unrecorded, smooth reposition to the exact experiment start.
        T=max(12.,1.875*np.linalg.norm(start-current)/.8)
        begin=time.monotonic(); period=1/catalog.SETPOINT_HZ
        while time.monotonic()-begin<T:
            q=min(1.,(time.monotonic()-begin)/T); node.publish(current+(start-current)*catalog.smoother(q),(start-current)*catalog.smoother_d(q)/T,tr.yaw[0],0); rclpy.spin_once(node,timeout_sec=0); time.sleep(period)
        node.stream(start,np.zeros(3),tr.yaw[0],0,2)
        node.mapper_start(start,tr.yaw[0])
        recorder=subprocess.Popen(record_command(raw_bag),stdout=(out/'recorder.log').open('w'),stderr=subprocess.STDOUT,start_new_session=True)
        node.stream(start,np.zeros(3),tr.yaw[0],0,2)
        if recorder.poll() is not None: raise RuntimeError('rosbag recorder exited during startup')
        stop=len(tr.t) if not a.smoke else min(len(tr.t),int(5*catalog.SETPOINT_HZ))
        acceleration=np.gradient(tr.v,tr.t,axis=0)
        use_acceleration_ff=True
        wall0=time.monotonic(); next_tick=wall0
        for i in range(stop):
            node.publish(tr.p[i],tr.v[i],tr.yaw[i],tr.yaw_rate[i],acceleration[i] if use_acceleration_ff else None); rclpy.spin_once(node,timeout_sec=0)
            if node.status and node.status.nav_state!=VehicleStatus.NAVIGATION_STATE_OFFBOARD: raise RuntimeError(f'offboard loss at sample {i}: status={node.status}; failsafe={node.failsafe}')
            if node.gt:
                node.stream(tr.p[i],tr.v[i],tr.yaw[i],tr.yaw_rate[i],0.00001,collect=True,t0=float(tr.t[i]))
            next_tick+=1/catalog.SETPOINT_HZ; time.sleep(max(0,next_tick-time.monotonic()))
        recorder.send_signal(signal.SIGINT); recorder.wait(timeout=30); recorder=None
        alignment=align_bag(raw_bag,bag)
        (out/'bag_alignment.json').write_text(json.dumps(alignment,indent=2,sort_keys=True)+'\n')
        with (out/'actual_trajectory.csv').open('w',newline='') as f:
            w=csv.writer(f); w.writerow(['t_s','cmd_xG','cmd_yG','cmd_zG','cmd_vxG','cmd_vyG','cmd_vzG','cmd_yaw','cmd_yaw_rate','actual_xG','actual_yG','actual_zG','actual_vxG','actual_vyG','actual_vzG','actual_yawG','nav_state']); w.writerows(node.samples)
        result={'status':'recorded','flight_id':a.flight_id,'sample_count':len(node.samples),'bag':str(bag),'raw_bag':str(raw_bag),'alignment':alignment,'smoke':a.smoke}
        (out/'execution_result.json').write_text(json.dumps(result,indent=2)+'\n'); print(json.dumps(result,indent=2))
    finally:
        if recorder and recorder.poll() is None: recorder.send_signal(signal.SIGINT); recorder.wait(timeout=20)
        node.destroy_node(); rclpy.shutdown()

if __name__=='__main__': main()
