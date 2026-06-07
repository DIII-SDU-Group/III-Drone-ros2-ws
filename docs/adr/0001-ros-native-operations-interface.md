# ROS-Native Operations Interface

Agent and GUI tooling need to execute the same primitive direct-operation commands without creating parallel control paths. We will expose the Operations Controller through a ROS-native Operations Interface, with GUI and MCP tooling implemented as clients of that interface. This keeps PX4 mode ownership, runtime state, command validation, and test coverage centered in the ROS runtime instead of split across GUI-specific or MCP-specific facades.
