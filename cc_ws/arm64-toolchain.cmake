# arm64-toolchain.cmake
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(Python_EXECUTABLE /usr/bin/python3)

# Specify the cross compiler
set(CMAKE_C_COMPILER /usr/bin/aarch64-linux-gnu-gcc)
set(CMAKE_CXX_COMPILER /usr/bin/aarch64-linux-gnu-g++)

# Specify the target sysroot
set(CMAKE_SYSROOT /arm64-sysroot)

# Specify where to find the libraries and includes for the target architecture
set(CMAKE_FIND_ROOT_PATH
    ${CMAKE_SYSROOT}
    ${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu
    ${CMAKE_SYSROOT}/usr/aarch64-linux-gnu
)

# # Additional search paths for libraries and includes
set(CMAKE_LIBRARY_PATH ${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu)
set(CMAKE_INCLUDE_PATH ${CMAKE_SYSROOT}/usr/include)

# # Set the prefix path for finding packages
set(CMAKE_PREFIX_PATH
    ${CMAKE_SYSROOT}/usr
    ${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu
    ${CMAKE_SYSROOT}/usr/aarch64-linux-gnu
    /home/iii/ws/install
)

# Adjust the default search behavior
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

set(GIT_EXECUTABLE /usr/bin/git)

set(CMAKE_EXE_LINKER_FLAGS "--sysroot=${CMAKE_SYSROOT} -L${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu")
set(CMAKE_SHARED_LINKER_FLAGS "--sysroot=${CMAKE_SYSROOT} -L${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu")


# # Specify the make program
# set(CMAKE_MAKE_PROGRAM /usr/bin/make)


# message(STATUS "Successfully loaded arm64-toolchain.cmake")