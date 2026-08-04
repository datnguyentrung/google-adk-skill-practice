def parse_skill_codes(message: str) -> tuple[list[str], str]:
    parts = message.strip().split()
    codes = []

    while parts and parts[0].startswith("/"):
        codes.append(parts.pop(0)[1:])

    return codes, " ".join(parts)
