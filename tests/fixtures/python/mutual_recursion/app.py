def main():
    print(is_even(4))


def is_even(n):
    return n == 0 or is_odd(n - 1)


def is_odd(n):
    return n != 0 and is_even(n - 1)
