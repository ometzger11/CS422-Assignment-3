import random

def generate_cookie():
    CHARSET = "abcdefghijklmnopqrstuvwxyz234567"
    NUM_CHARS = 36
    
    low = 0
    high = len(CHARSET) - 1

    chips = []
    
    for i in range(NUM_CHARS):
        chips.append(CHARSET[random.randint(low, high)])

    chips.append('\0')

    return "".join(chips)

if __name__ == "__main__":
    print("Sample cookie: " + generate_cookie())
