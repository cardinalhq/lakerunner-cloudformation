import pytest
import json
from unittest.mock import patch


class TestServicesTemplateSimple:
    """Simplified test cases for the Services CloudFormation template that avoid complex template generation"""

    def test_load_service_config_function(self):
        """Test that the load_service_config function exists and is importable"""
        from lakerunner_services import load_service_config
        assert callable(load_service_config)

    def test_create_services_template_function(self):
        """Test that the create_services_template function exists and is importable"""
        from lakerunner_services import create_services_template
        assert callable(create_services_template)

    @patch('lakerunner_services.load_service_config')
    def test_template_generation_basic(self, mock_load_config):
        """Test basic template generation with minimal mock config"""
        # Minimal config that should not cause errors
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "lakerunner": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_services import create_services_template
        
        # This should not raise an exception
        template = create_services_template()
        assert template is not None

    @patch('lakerunner_services.load_service_config')
    def test_template_has_basic_structure(self, mock_load_config):
        """Test that generated template has basic CloudFormation structure"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "lakerunner": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        # Check basic CloudFormation structure
        assert "Description" in template_dict
        assert "Parameters" in template_dict
        assert "Resources" in template_dict

    @patch('lakerunner_services.load_service_config')
    def test_template_description_correct(self, mock_load_config):
        """Test that template description is correct"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "lakerunner": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        expected_desc = "Lakerunner Services: ECS services, task definitions, IAM roles, and ALB integration"
        assert template_dict["Description"] == expected_desc

    @patch('lakerunner_services.load_service_config')
    def test_commoninfra_parameter_exists(self, mock_load_config):
        """Test that CommonInfraStackName parameter exists"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "lakerunner": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        # Should have CommonInfraStackName parameter for imports
        assert "CommonInfraStackName" in template_dict["Parameters"]

    @patch('lakerunner_services.load_service_config')
    def test_image_parameters_exist(self, mock_load_config):
        """Test that container image parameters exist"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "lakerunner": "test:latest",
                "query_api": "test:latest",
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        parameters = template_dict["Parameters"]

        # Image is now hard-coded from defaults, not a parameter
        assert "GoServicesImage" not in parameters, "GoServicesImage should not be a parameter"

    @patch('lakerunner_services.load_service_config')
    def test_signal_type_parameters_removed(self, mock_load_config):
        """Test that signal type parameters have been removed (all types always enabled)"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "lakerunner": "test:latest",
                "query_api": "test:latest",
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        parameters = template_dict["Parameters"]

        # Signal type parameters should not exist
        assert "EnableLogs" not in parameters
        assert "EnableMetrics" not in parameters
        assert "EnableTraces" not in parameters

    @patch('lakerunner_services.load_service_config')
    def test_signal_type_conditions_removed(self, mock_load_config):
        """Test that signal type conditions have been removed (all types always enabled)"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "lakerunner": "test:latest",
                "query_api": "test:latest",
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        conditions = template_dict.get("Conditions", {})

        # Signal type conditions should not exist
        assert "CreateLogsServices" not in conditions
        assert "CreateMetricsServices" not in conditions
        assert "CreateTracesServices" not in conditions

    @patch('lakerunner_services.load_service_config')
    def test_signal_type_service_creation(self, mock_load_config):
        """Test that services are conditionally created based on signal type"""
        mock_load_config.return_value = {
            "services": {
                "test-logs-service": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "test-metrics-service": {
                    "signal_type": "metrics",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "test-traces-service": {
                    "signal_type": "traces",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "test-common-service": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                }
            },
            "images": {
                "lakerunner": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        resources = template_dict.get("Resources", {})

        # Signal-type services should have no conditions (always created)
        for resource_prefix in ["LogGroup", "TaskDef", "Service"]:
            for svc in ["TestLogsService", "TestMetricsService", "TestTracesService", "TestCommonService"]:
                assert "Condition" not in resources[f"{resource_prefix}{svc}"]

        # Outputs should have no conditions for signal-type services
        outputs = template_dict.get("Outputs", {})
        for svc in ["TestLogsService", "TestMetricsService", "TestTracesService", "TestCommonService"]:
            assert "Condition" not in outputs[f"Service{svc}Arn"]

    @patch('lakerunner_services.load_service_config')
    def test_query_services_have_cpu_memory_replicas_params(self, mock_load_config):
        """Test that query-api and query-worker have CPU, Memory, and Replicas parameters"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-query-api": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-api"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-query-worker": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-worker"],
                    "cpu": 2048,
                    "memory_mib": 8192,
                    "replicas": 4,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"lakerunner": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # Query API parameters
        assert "QueryApiReplicas" in parameters
        assert "QueryApiCpu" in parameters
        assert "QueryApiMemory" in parameters
        assert parameters["QueryApiReplicas"]["Default"] == "2"
        assert parameters["QueryApiCpu"]["Default"] == "1024"
        assert parameters["QueryApiMemory"]["Default"] == "2048"

        # Query Worker parameters
        assert "QueryWorkerReplicas" in parameters
        assert "QueryWorkerCpu" in parameters
        assert "QueryWorkerMemory" in parameters
        assert parameters["QueryWorkerReplicas"]["Default"] == "4"
        assert parameters["QueryWorkerCpu"]["Default"] == "2048"
        assert parameters["QueryWorkerMemory"]["Default"] == "8192"

    @patch('lakerunner_services.load_service_config')
    def test_process_services_have_memory_replicas_params(self, mock_load_config):
        """Test that process services have Memory and Replicas parameters (not CPU)"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-process-logs": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "process-logs"],
                    "cpu": 1024,
                    "memory_mib": 4096,
                    "replicas": 4,
                    "autoscaling": {"min_replicas": 1, "max_replicas": 4},
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-process-metrics": {
                    "signal_type": "metrics",
                    "command": ["/app/bin/lakerunner", "process-metrics"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "autoscaling": {"min_replicas": 1, "max_replicas": 2},
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-process-traces": {
                    "signal_type": "traces",
                    "command": ["/app/bin/lakerunner", "process-traces"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "autoscaling": {"min_replicas": 1, "max_replicas": 2},
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"lakerunner": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # Process logs - has memory and replicas, not CPU
        assert "ProcessLogsReplicas" in parameters
        assert "ProcessLogsMemory" in parameters
        assert "ProcessLogsCpu" not in parameters
        assert parameters["ProcessLogsReplicas"]["Default"] == "4"
        assert parameters["ProcessLogsMemory"]["Default"] == "4096"

        # Process metrics - has memory and replicas, not CPU
        assert "ProcessMetricsReplicas" in parameters
        assert "ProcessMetricsMemory" in parameters
        assert "ProcessMetricsCpu" not in parameters

        # Process traces - has memory and replicas, not CPU
        assert "ProcessTracesReplicas" in parameters
        assert "ProcessTracesMemory" in parameters
        assert "ProcessTracesCpu" not in parameters

    @patch('lakerunner_services.load_service_config')
    def test_replicas_only_services_have_replicas_param_only(self, mock_load_config):
        """Test that pubsub has only Replicas parameter (no CPU/Memory)"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-pubsub-sqs": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "pubsub", "sqs"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"lakerunner": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # Pubsub SQS - has replicas only
        assert "PubsubSqsReplicas" in parameters
        assert "PubsubSqsMemory" not in parameters
        assert "PubsubSqsCpu" not in parameters

    @patch('lakerunner_services.load_service_config')
    def test_sweeper_monitor_have_no_params(self, mock_load_config):
        """Test that sweeper and monitoring use YAML values only (no parameters)"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-sweeper": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "sweeper"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-monitoring": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "monitoring", "serve"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"lakerunner": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # Sweeper - no parameters
        assert "SweeperReplicas" not in parameters
        assert "SweeperMemory" not in parameters
        assert "SweeperCpu" not in parameters

        # Monitoring - no parameters
        assert "MonitoringReplicas" not in parameters
        assert "MonitoringMemory" not in parameters
        assert "MonitoringCpu" not in parameters

    @patch('lakerunner_services.load_service_config')
    def test_task_definitions_use_correct_values(self, mock_load_config):
        """Test that task definitions use Ref for params or hardcoded YAML values appropriately"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-query-api": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-api"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-process-logs": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "process-logs"],
                    "cpu": 1024,
                    "memory_mib": 4096,
                    "replicas": 4,
                    "autoscaling": {"min_replicas": 1, "max_replicas": 4},
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-sweeper": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "sweeper"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"lakerunner": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]

        # Query API - CPU and Memory should be Ref
        query_api_task = resources["TaskDefLakerunnerQueryApi"]["Properties"]
        assert query_api_task["Cpu"] == {"Ref": "QueryApiCpu"}
        assert query_api_task["Memory"] == {"Ref": "QueryApiMemory"}

        # Process Logs - CPU should be hardcoded, Memory should be Ref
        process_logs_task = resources["TaskDefLakerunnerProcessLogs"]["Properties"]
        assert process_logs_task["Cpu"] == "1024"
        assert process_logs_task["Memory"] == {"Ref": "ProcessLogsMemory"}

        # Sweeper - CPU and Memory should be hardcoded from YAML
        sweeper_task = resources["TaskDefLakerunnerSweeper"]["Properties"]
        assert sweeper_task["Cpu"] == "256"
        assert sweeper_task["Memory"] == "512"

    @patch('lakerunner_services.load_service_config')
    def test_ecs_services_use_correct_desired_count(self, mock_load_config):
        """Test that ECS services use Ref for replicas or hardcoded values appropriately"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-query-api": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-api"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-pubsub-sqs": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "pubsub", "sqs"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-sweeper": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "sweeper"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"lakerunner": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]

        # Query API - DesiredCount should be Ref
        query_api_svc = resources["ServiceLakerunnerQueryApi"]["Properties"]
        assert query_api_svc["DesiredCount"] == {"Ref": "QueryApiReplicas"}

        # Pubsub SQS - DesiredCount should be Ref (replicas-only service)
        pubsub_svc = resources["ServiceLakerunnerPubsubSqs"]["Properties"]
        assert pubsub_svc["DesiredCount"] == {"Ref": "PubsubSqsReplicas"}

        # Sweeper - DesiredCount should be hardcoded from YAML
        sweeper_svc = resources["ServiceLakerunnerSweeper"]["Properties"]
        assert sweeper_svc["DesiredCount"] == "1"

    @patch('lakerunner_services.load_service_config')
    def test_autoscaling_parameters_removed(self, mock_load_config):
        """Test that auto-scaling parameters have been removed"""
        mock_load_config.return_value = {
            "services": {},
            "images": {"lakerunner": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]

        # All auto-scaling parameters should be removed
        assert "AutoScalingCPUTarget" not in parameters
        assert "AutoScalingScaleOutCooldown" not in parameters
        assert "AutoScalingScaleInCooldown" not in parameters
        assert "EnableAutoScaling" not in parameters
        assert "AutoScalingMaxReplicas" not in parameters

    @patch('lakerunner_services.load_service_config')
    def test_autoscaling_uses_signal_type_conditions(self, mock_load_config):
        """Test that auto-scaling uses the signal type conditions (always on when signal enabled)"""
        mock_load_config.return_value = {
            "services": {},
            "images": {"lakerunner": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        conditions = template_dict.get("Conditions", {})

        # No signal-type or auto-scaling conditions should exist
        assert "AutoScalingEnabled" not in conditions
        assert "AutoScaleLogsServices" not in conditions
        assert "AutoScaleMetricsServices" not in conditions
        assert "AutoScaleTracesServices" not in conditions
        assert "CreateLogsServices" not in conditions
        assert "CreateMetricsServices" not in conditions
        assert "CreateTracesServices" not in conditions

    @patch('lakerunner_services.load_service_config')
    def test_autoscaling_not_created_for_non_worker_services(self, mock_load_config):
        """Test that auto-scaling is NOT created for query, pubsub, sweeper, monitoring"""
        mock_load_config.return_value = {
            "services": {
                "lakerunner-query-api": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "query-api"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 2,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-pubsub-sqs": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "pubsub", "sqs"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                },
                "lakerunner-sweeper": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "sweeper"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["/app/bin/lakerunner", "sysinfo"]},
                    "environment": {}
                }
            },
            "images": {"lakerunner": "test:latest"}
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]

        # No auto-scaling resources for non-worker services
        assert "ScalableTargetLakerunnerQueryApi" not in resources
        assert "ScalingPolicyCpuLakerunnerQueryApi" not in resources
        assert "ScalableTargetLakerunnerPubsubSqs" not in resources
        assert "ScalingPolicyCpuLakerunnerPubsubSqs" not in resources
        assert "ScalableTargetLakerunnerSweeper" not in resources
        assert "ScalingPolicyCpuLakerunnerSweeper" not in resources

