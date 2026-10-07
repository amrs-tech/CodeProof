def greeting(name):
    prefix = f"Hello"
    return f"{prefix}, {name}!"


def remember(value, history=[]):
    """A fragile shared default that CodeProof should flag for review."""
    history.append(value)
    return history
