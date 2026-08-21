import re

def clean_and_space_burmese(text: str) -> str:
    """Adds space before consonants to slow voice synthesis down and prevent rushing."""
    text = text.replace('​', '') # Remove zero-width spaces
    # Lookbehind for [vowels, asat, tone marks] -> lookahead for [consonant]
    pattern = re.compile(r'(?<=[ါ-ှ])(?=[က-ဪ])')
    spaced = pattern.sub(' ', text)
    return re.sub(r'\s+', ' ', spaced).strip()

test_string = "မြန်မာဘာသာစကားသည် အလွန်လှပသောဘာသာစကားဖြစ်ပါသည်။"
spaced = clean_and_space_burmese(test_string)

print(spaced)
