// Compiled by the ARM64 release qualification to prove that CMake's relative
// colcon source spelling cannot leak the offboard workspace into an artifact.
extern "C" const char *iii_path_map_probe()
{
  return __FILE__;
}
