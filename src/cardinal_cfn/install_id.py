"""Derivation helpers for the per-install identifier.

InstallId* are computed in the root template only. They are then propagated
as parameters to every nested child stack. Children must never call these
functions directly: Ref(AWS::StackId) inside a child returns the *child's*
stack id, not the root's.
"""

from troposphere import Join, Ref, Select, Split


def _stack_uuid():
    """Return the UUID portion of AWS::StackId (an ARN)."""
    return Select(2, Split("/", Ref("AWS::StackId")))


def install_id_short():
    """8-char hex group: the first segment of the StackId UUID."""
    return Select(0, Split("-", _stack_uuid()))


def install_id_long():
    """12-char hex: the first two segments of the StackId UUID joined."""
    uuid = _stack_uuid()
    return Join(
        "",
        [
            Select(0, Split("-", uuid)),
            Select(1, Split("-", uuid)),
        ],
    )
