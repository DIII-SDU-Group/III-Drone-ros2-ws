import os

from setuptools import find_packages, setup

package_name = "iii_drone_powerline_slam"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        (os.path.join("share", package_name), ["package.xml", "README.md"]),
    ],
    # The executable is a shell wrapper: the node runs inside the powerline SLAM estimator environment
    # (scripts/workspace/setup_powerline_slam_estimator_env.sh), not the plain ROS interpreter.
    scripts=["scripts/powerline_slam_node"],
    install_requires=["setuptools"],
    tests_require=["pytest"],
    zip_safe=True,
    maintainer="Frederik Falk Nyboe",
    maintainer_email="ffn@sdu.dk",
    description="Lifecycle-managed powerline SLAM perception backend (passive output).",
    license="proprietary",
)
