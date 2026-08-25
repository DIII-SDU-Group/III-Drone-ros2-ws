set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(CMAKE_SYSROOT "/opt/iii/sysroot" CACHE PATH "Canonical immutable ARM64 sysroot")

set(CMAKE_C_COMPILER "/usr/bin/aarch64-linux-gnu-gcc-13")
set(CMAKE_CXX_COMPILER "/usr/bin/aarch64-linux-gnu-g++-13")
set(CMAKE_C_COMPILER_TARGET "aarch64-linux-gnu")
set(CMAKE_CXX_COMPILER_TARGET "aarch64-linux-gnu")

# Python and git are builder tools. Target Python headers/libraries are found
# exclusively through the sysroot and never replaced with host symlinks.
set(Python3_EXECUTABLE "/usr/bin/python3" CACHE FILEPATH "Builder Python")
set(GIT_EXECUTABLE "/usr/bin/git" CACHE FILEPATH "Builder git")
set(CMAKE_FIND_ROOT_PATH
    "${CMAKE_SYSROOT}"
    "${CMAKE_SYSROOT}/opt/ros/jazzy"
    "${CMAKE_SYSROOT}/usr"
    "${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu")
set(CMAKE_PREFIX_PATH
    "${CMAKE_SYSROOT}/opt/ros/jazzy"
    "${CMAKE_SYSROOT}/usr")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
