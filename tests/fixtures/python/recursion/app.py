def main():
    print(factorial(5))


def factorial(n):
    return n * factorial(n - 1) if n > 1 else 1
