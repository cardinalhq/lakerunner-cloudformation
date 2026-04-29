"""config.yaml nested stack: SSM parameters and shared secrets."""

import yaml

from troposphere import Template, Ref, Output, If, Equals, Not
from troposphere.ssm import Parameter as SSMParameter
from troposphere.secretsmanager import GenerateSecretString, Secret

from cardinal_cfn.naming import ssm_param_name, secret_name, cardinal_tags
from cardinal_cfn.parameters import add_install_id_parameters, add_no_echo_parameter
from cardinal_cfn.defaults import load_defaults
from cardinal_cfn.policies import apply_policy


def _yaml_block(value) -> str:
    return yaml.safe_dump(value, default_flow_style=False, sort_keys=False)


def build() -> Template:
    t = Template()
    t.set_description("Cardinal config: SSM parameters and shared secrets.")

    add_install_id_parameters(t)
    add_no_echo_parameter(t, "LicenseData", description="License JSON content.")
    add_no_echo_parameter(t, "ApiKeysOverride", description="Optional API keys YAML override.")
    add_no_echo_parameter(t, "StorageProfilesOverride", description="Optional storage profiles YAML override.")

    t.add_condition("HasApiKeysOverride", Not(Equals(Ref("ApiKeysOverride"), "")))
    t.add_condition("HasStorageProfilesOverride", Not(Equals(Ref("StorageProfilesOverride"), "")))

    defaults = load_defaults()
    api_keys_default_yaml = _yaml_block(defaults["api_keys"])
    storage_profiles_default_yaml = _yaml_block(defaults["storage_profiles"])

    api_keys_param = t.add_resource(
        SSMParameter(
            "ApiKeysParam",
            Name=ssm_param_name(key="api-keys"),
            Type="String",
            Value=If("HasApiKeysOverride", Ref("ApiKeysOverride"), api_keys_default_yaml),
            Description="Cardinal API keys (YAML).",
        )
    )

    storage_profiles_param = t.add_resource(
        SSMParameter(
            "StorageProfilesParam",
            Name=ssm_param_name(key="storage-profiles"),
            Type="String",
            Value=If("HasStorageProfilesOverride", Ref("StorageProfilesOverride"), storage_profiles_default_yaml),
            Description="Cardinal storage profiles (YAML).",
        )
    )

    # Externally referenced secret — Retain on delete.
    license_secret = t.add_resource(
        Secret(
            "LicenseSecret",
            Name=secret_name(purpose="license"),
            SecretString=Ref("LicenseData"),
            Tags=cardinal_tags(component="config", role="license-secret"),
        )
    )
    apply_policy(license_secret, "license-secret")

    # Auto-generated, AWS-managed name — Delete on delete.
    internal_keys = t.add_resource(
        Secret(
            "InternalServiceKeysSecret",
            GenerateSecretString=GenerateSecretString(
                SecretStringTemplate='{"name":"internal"}',
                GenerateStringKey="key",
                ExcludePunctuation=True,
            ),
            Tags=cardinal_tags(component="config", role="internal-service-keys"),
        )
    )
    apply_policy(internal_keys, "internal-service-keys-secret")

    # Externally referenced secret — Retain on delete.
    admin_key = t.add_resource(
        Secret(
            "AdminApiKeySecret",
            Name=secret_name(purpose="admin-api-key"),
            GenerateSecretString=GenerateSecretString(
                SecretStringTemplate='{"name":"admin"}',
                GenerateStringKey="key",
                ExcludePunctuation=True,
            ),
            Tags=cardinal_tags(component="config", role="admin-api-key"),
        )
    )
    apply_policy(admin_key, "admin-api-key-secret")

    t.add_output(Output("LicenseSecretArn", Value=Ref(license_secret)))
    t.add_output(Output("InternalServiceKeysSecretArn", Value=Ref(internal_keys)))
    t.add_output(Output("AdminApiKeySecretArn", Value=Ref(admin_key)))
    t.add_output(Output("ApiKeysParamName", Value=Ref(api_keys_param)))
    t.add_output(Output("StorageProfilesParamName", Value=Ref(storage_profiles_param)))

    return t


if __name__ == "__main__":
    print(build().to_yaml())
