import os
import sys
import pytest

# Add src directory to Python path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

@pytest.fixture
def sample_parameters():
    """Sample parameters for testing templates"""
    return {
        'Environment': 'test',
        'ClusterName': 'test-cluster',
        'DatabaseInstanceClass': 'db.t3.micro',
        'DatabaseAllocatedStorage': '20',
        'DatabaseEngineVersion': '13.15',
        'VpcCidr': '10.0.0.0/16',
        'PrivateSubnet1Cidr': '10.0.3.0/24',
        'PrivateSubnet2Cidr': '10.0.4.0/24'
    }