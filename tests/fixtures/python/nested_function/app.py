def main():
    outer()


def outer():
    def inner():
        print("inner")

    inner()
