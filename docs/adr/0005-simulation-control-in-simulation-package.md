# Simulation Control Belongs In The Simulation Package

Gazebo camera control, simulation viewpoints, and image snapshot capture are simulation-domain capabilities, so they belong in `III-Drone-Simulation` rather than the mission operations package. Agent tooling may compose simulation control with operations and PX4 command clients, but the Gazebo-specific implementation should stay with the simulation assets and integration code.
