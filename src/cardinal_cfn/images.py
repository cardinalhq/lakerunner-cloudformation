"""Image-override parameters.

Every child stack that runs a container declares an image-override parameter
for each container, with a default pointing to public ECR. Air-gapped
customers override at deploy time to point to a private mirror.
"""

from troposphere import Parameter, Ref, Template


def add_image_override(
    t: Template,
    *,
    name: str,
    default: str,
    description: str,
) -> Ref:
    """Declare an image-override Parameter and return a Ref to it."""
    p = t.add_parameter(
        Parameter(
            name,
            Type="String",
            Default=default,
            Description=description,
        )
    )
    return Ref(p)
