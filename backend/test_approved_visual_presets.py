import copy
import unittest

from backend.approved_visual_presets import approved_visual_preset, visual_content_signature


def _approved_render_input():
    segments = [
        {
            "id": "A1",
            "text": "ဂိုဒေါင်ထဲမှာ ပစ္စည်း ၁၂ ခု ကျန်ပါသေးတယ်။ ဒါပေမယ့် ကွန်ပျူတာစာရင်းထဲမှာတော့ ၂ ခုပဲရှိတယ်လို့ ပြနေပါတယ်။",
            "visual": {"screen_text": ["အပြင်လက်ကျန်: ၁၂ ခု", "စနစ်ထဲကစာရင်း: ၂ ခု"]},
        },
        {
            "id": "A2",
            "text": "ဒီအချိန်မှာ AI Agent လို့ခေါ်တဲ့ အလိုအလျောက် အလုပ်ကူလုပ်ပေးတဲ့ စနစ်က ပစ္စည်းနည်းပြီဆိုပြီး ထပ်ဝယ်လိုက်ပါတယ်။ ဒါကြောင့် မလိုအပ်တဲ့ ပစ္စည်းတွေ အများကြီး ရောက်လာပါတယ်။",
            "visual": {"screen_text": ["AI က အလိုအလျောက်မှာယူခြင်း", "မလိုအပ်ဘဲ ပစ္စည်းများလာခြင်း"]},
        },
        {
            "id": "A3",
            "text": "ပြဿနာက AI မကောင်းလို့ မဟုတ်ပါဘူး။ AI က ကွန်ပျူတာထဲက အချက်အလက်ကိုပဲ မြင်ရတာမို့ အပြင်မှာ တကယ်ဖြစ်နေတာကို မသိလို့ပါ။",
            "visual": {"screen_text": ["AI က ဒေတာကိုပဲမြင်ရသည်", "အပြင်ကအခြေအနေကိုမသိပါ"]},
        },
        {
            "id": "A4",
            "text": "ဒါကြောင့် ပိုက်ဆံကုန်မယ့် ကိစ္စတွေကို AI တစ်ခုတည်းနဲ့ အဆုံးအဖြတ် မပေးသင့်ပါဘူး။ AI ကို ဘယ်လောက်ဝယ်သင့်လဲ အကြံပြုခိုင်းပြီး လူက နောက်ဆုံး ပြန်စစ်ကာ အတည်ပြုပေးရပါမယ်။",
            "visual": {"screen_text": ["AI က အကြံပြုသည်", "လူက အတည်ပြုသည်"]},
        },
        {
            "id": "A5",
            "text": "ကောင်းတဲ့ AI ဆိုတာ မသေချာတဲ့အခါ ရပ်ပြီး ဆုံးဖြတ်ချက်မချခင် လူကို အရင်မေးတတ်တဲ့ AI ဖြစ်ပါတယ်။ သင့်လုပ်ငန်းမှာရော AI ကို လုံးဝမအပ်သင့်သေးတဲ့ အလုပ်တစ်ခုက ဘာဖြစ်မလဲ။",
            "visual": {"screen_text": ["မသေချာလျှင် လူကိုမေးပါ", "လူနှင့် AI ပူးပေါင်းဆောင်ရွက်မှု"]},
        },
    ]
    return {"title": "ဂိုဒေါင်ထဲက AI ပြဿနာ", "language": "my-MM", "segments": segments}


class TestApprovedVisualPresets(unittest.TestCase):
    def setUp(self):
        self.approved = _approved_render_input()

    def test_exact_approved_content_resolves_preset(self):
        self.assertIsNotNone(approved_visual_preset(self.approved))

    def test_timing_and_voice_do_not_change_semantic_signature(self):
        other_voice = copy.deepcopy(self.approved)
        other_voice.update({"audioSrc": "other-voice.wav", "durationInFrames": 9999})
        self.assertEqual(visual_content_signature(self.approved), visual_content_signature(other_voice))

    def test_unrelated_content_cannot_receive_inventory_preset(self):
        unrelated = copy.deepcopy(self.approved)
        unrelated["segments"][0]["text"] = "Different future topic"
        self.assertIsNone(approved_visual_preset(unrelated))


if __name__ == "__main__":
    unittest.main()
