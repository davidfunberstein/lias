class ResetException(Exception):
    """חריגה המאפשרת לחזור לתפריט הראשי בכל שלב."""
    pass

def check_reset(user_input: str):
    """בודק אם המשתמש הקיש 'reset' וזורק חריגה במידת הצורך."""
    if user_input.lower().strip() == 'reset':
        raise ResetException()