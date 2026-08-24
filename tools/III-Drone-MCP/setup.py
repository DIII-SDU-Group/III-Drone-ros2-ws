from setuptools import find_packages, setup


setup(
    name="iii-drone-mcp",
    version="0.1.0",
    description="Workspace-level MCP server for III-Drone agent operations",
    packages=find_packages(),
    install_requires=[
        "PyYAML",
    ],
    scripts=[
        "bin/iii_drone_mcp_batch",
        "bin/iii_drone_mcp_call",
        "bin/iii_drone_mcp_mission_deploy",
        "bin/iii_drone_mcp_server",
        "bin/run_mcp_observation_tests.sh",
    ],
    entry_points={
        "console_scripts": [
            "iii-drone-mcp-server=iii_drone_mcp.mcp_server:main",
            "iii-drone-mcp-call=iii_drone_mcp.mcp_call:main",
            "iii-drone-mcp-batch=iii_drone_mcp.mcp_batch:main",
            "iii-drone-mcp-mission-deploy=iii_drone_mcp.mission_deploy_workflow:main",
            "iii-drone-mcp-mission-scenario-suite=iii_drone_mcp.mission_scenario_suite:main",
            "iii-drone-mcp-observation-tests=iii_drone_mcp.observation_test_suite:main",
        ],
    },
)
