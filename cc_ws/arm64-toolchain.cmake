set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(CMAKE_SYSROOT "/opt/iii/sysroot" CACHE PATH "Canonical immutable ARM64 sysroot")

set(CMAKE_C_COMPILER "/usr/bin/aarch64-linux-gnu-gcc-13")
set(CMAKE_CXX_COMPILER "/usr/bin/aarch64-linux-gnu-g++-13")
set(CMAKE_C_COMPILER_TARGET "aarch64-linux-gnu")
set(CMAKE_CXX_COMPILER_TARGET "aarch64-linux-gnu")
set(CMAKE_AR "/usr/bin/aarch64-linux-gnu-ar" CACHE FILEPATH "Target archiver" FORCE)
set(CMAKE_RANLIB "/usr/bin/aarch64-linux-gnu-ranlib" CACHE FILEPATH "Target ranlib" FORCE)
set(CMAKE_STRIP "/usr/bin/aarch64-linux-gnu-strip" CACHE FILEPATH "Target strip" FORCE)
# CMake resolves the generator program before applying root-path modes. Pin it
# to the amd64 builder so the ARM64 sysroot's gmake is never executed.
set(CMAKE_MAKE_PROGRAM "/usr/bin/make" CACHE FILEPATH "Builder make" FORCE)
set(CMAKE_C_COMPILER_LAUNCHER "/usr/bin/ccache" CACHE FILEPATH "Pinned compiler cache" FORCE)
set(CMAKE_CXX_COMPILER_LAUNCHER "/usr/bin/ccache" CACHE FILEPATH "Pinned compiler cache" FORCE)

# Prevent source/cache locations from being embedded by __FILE__, debug data,
# or compiler-generated metadata even when a package adds its own flags.
set(III_BUILD_PATH_MAP
    "-ffile-prefix-map=/home/iii/ws=/usr/src/iii"
    "-fdebug-prefix-map=/home/iii/ws=/usr/src/iii"
    "-fmacro-prefix-map=/home/iii/ws=/usr/src/iii"
    # Colcon invokes CMake builds below /cache/build/<package>; CMake commonly
    # passes workspace sources as this normalized relative spelling.
    "-ffile-prefix-map=../../../home/iii/ws=/usr/src/iii"
    "-fdebug-prefix-map=../../../home/iii/ws=/usr/src/iii"
    "-fmacro-prefix-map=../../../home/iii/ws=/usr/src/iii"
    "-ffile-prefix-map=/opt/iii/sysroot=/usr/src/iii-sysroot"
    "-fdebug-prefix-map=/opt/iii/sysroot=/usr/src/iii-sysroot"
    "-fmacro-prefix-map=/opt/iii/sysroot=/usr/src/iii-sysroot"
    "-ffile-prefix-map=/cache=/usr/src/iii-cache"
    "-fdebug-prefix-map=/cache=/usr/src/iii-cache")
string(JOIN " " III_BUILD_PATH_MAP_FLAGS ${III_BUILD_PATH_MAP})
set(CMAKE_C_FLAGS_INIT "${III_BUILD_PATH_MAP_FLAGS}")
set(CMAKE_CXX_FLAGS_INIT "${III_BUILD_PATH_MAP_FLAGS}")

# Python and git are builder tools. Target Python headers/libraries are found
# exclusively through the sysroot and never replaced with host symlinks.
set(Python3_EXECUTABLE "/usr/bin/python3" CACHE FILEPATH "Builder Python")
set(Python3_NumPy_INCLUDE_DIR "/opt/iii/ros-build-tools/numpy/core/include" CACHE PATH "Pinned build-tool NumPy headers" FORCE)
set(Python3_NumPy_INCLUDE_DIRS "/opt/iii/ros-build-tools/numpy/core/include" CACHE PATH "Pinned build-tool NumPy headers" FORCE)
set(GIT_EXECUTABLE "/usr/bin/git" CACHE FILEPATH "Builder git")
set(CMAKE_FIND_ROOT_PATH
    "${CMAKE_SYSROOT}"
    "${CMAKE_SYSROOT}/opt/ros/jazzy"
    "${CMAKE_SYSROOT}/usr"
    "${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu")
# CMAKE_PREFIX_PATH is initialized from the environment. Colcon prepends each
# freshly installed dependency; entrypoint_cc.sh appends the target ROS prefix.
# Do not overwrite that evolving isolated-install chain here.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE BOTH)

# GNU ld does not reinterpret absolute Debian alternatives symlinks relative
# to CMAKE_SYSROOT while resolving a shared object's DT_NEEDED closure. Search
# the immutable target's real BLAS/LAPACK directories explicitly at link time;
# rpath-link does not become a runtime RUNPATH in release binaries.
set(III_TARGET_RPATH_LINK
    "-Wl,-rpath-link,${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu"
    "-Wl,-rpath-link,${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu/blas"
    "-Wl,-rpath-link,${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu/lapack"
    "-Wl,-rpath-link,${CMAKE_SYSROOT}/opt/ros/jazzy/lib"
    "-Wl,-rpath-link,${CMAKE_SYSROOT}/opt/ros/jazzy/lib/aarch64-linux-gnu")
string(JOIN " " III_TARGET_RPATH_LINK_FLAGS ${III_TARGET_RPATH_LINK})
set(CMAKE_EXE_LINKER_FLAGS_INIT "${III_TARGET_RPATH_LINK_FLAGS}")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "${III_TARGET_RPATH_LINK_FLAGS}")
