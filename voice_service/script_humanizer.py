"""
Script Humanizer — turns robotic text into natural spoken Burmese
using the "write for the ear" technique (biggest win for natural voice).
"""

import re

# FYF Brand Writing Rules (from BRAND_FOUNDATION.md)
# 1. Open with a recognizable moment, tension, or consequence — not a definition
# 2. One concrete example before the general lesson
# 3. Short, varied sentences, natural Burmese rhythm
# 4. No generic openings like "ယနေ့ခေတ်မှာ AI ဟာ..."
# 5. No AI-writing signals (revolutionary, seamless, etc.)

# Common robotic openings to replace
ROBOTIC_OPENINGS = {
    "ယနေ့ခေတ်မှာ AI ဟာ": "AI ကို လက်တွေ့သုံးကြည့်တဲ့အခါ",
    "ယနေ့ခေတ်တွင် AI သည်": "AI ကို လက်တွေ့သုံးကြည့်တဲ့အခါ",
    "လက်ရှိခေတ်မှာ AI ဟာ": "AI ကို လက်တွေ့သုံးကြည့်တဲ့အခါ",
    "အခုခေတ်မှာ AI ဟာ": "AI ကို လက်တွေ့သုံးကြည့်တဲ့အခါ",
    "ပထမဦးစွာ": "အရင်ဆုံး",
    "ပထမဆုံးအနေနဲ့": "အရင်ဆုံး",
    "နောက်ဆုံးအနေနဲ့": "နောက်ဆုံးမှာတော့",
}

# AI-writing signal phrases to remove
AI_SIGNALS = [
    "revolutionary", "game-changing", "seamless", "unlock",
    "transform your business", "work smarter, not harder",
]

# Burmese sentence patterns
def humanize_burmese(text: str) -> str:
    """Convert AI-ish Burmese text into spoken, natural-sounding Burmese.

    Techniques:
    1. Short sentences (breathe at commas/periods)
    2. Mix sentence lengths
    3. Add natural interjections/pauses
    4. Spoken style (not blog style)
    """
    text = text.strip()

    # Replace robotic openings with brand-style openings
    for robotic, human in ROBOTIC_OPENINGS.items():
        text = text.replace(robotic, human, 1)

    # Remove AI-writing signal phrases
    for signal in AI_SIGNALS:
        text = text.replace(signal, "")

    # Simple, safe approach: ensure sentences end with း or ။ and are short
    # Don't split words — just ensure proper sentence boundaries
    sentences = re.split(r'(?<=[။!?])\s*', text)
    chunks = [s.strip() for s in sentences if s.strip()]

    # Join naturally with commas where sentences are long
    result = ' '.join(chunks)

    # Add pause markers (…) between the first two sentences for natural rhythm
    first_end = result.find('။')
    if first_end > 0:
        rest = result[first_end+1:].strip()
        if rest:
            result = result[:first_end+1] + ' ... ' + rest

    return result

def add_emotion_hint(style: str = "casual") -> str:
    """Add natural emotion hint prefix for voice synthesis styling."""
    hints = {
        "casual": "(speak naturally, like a real person talking casually to a friend, relaxed and warm)",
        "friendly": "(speak in a friendly, warm and welcoming way, like greeting a good friend)",
        "authoritative": "(speak with calm authority, clear and confident, like an expert explaining simply)",
        "excited": "(speak with genuine excitement and enthusiasm, like sharing something amazing)",
        "thoughtful": "(speak thoughtfully, carefully explaining step by step, like a patient teacher)",
    }
    return hints.get(style, hints["casual"])

if __name__ == "__main__":
    # Test
    robotic = "ယနေ့ခေတ်မှာ AI ဟာ လူတွေရဲ့ အလုပ်တွေကို အလိုအလျောက် လုပ်ပေးနိုင်တဲ့ နည်းပညာတစ်ခုဖြစ်လာပါတယ်။ ပထမဆုံးအနေနဲ့ AI ကို ဘယ်လိုအသုံးပြုရမလဲဆိုတာ သိထားဖို့လိုပါတယ်။"
    human = humanize_burmese(robotic)
    print("ROBOTIC:", robotic)
    print()
    print("HUMAN:", human)
    print()
    print("WITH EMOTION:", add_emotion_hint("casual") + " " + human)
