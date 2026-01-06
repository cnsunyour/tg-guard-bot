"""表情验证映射表

包含文字描述到表情符号的映射，用于 Emoji 验证类型。
每个映射包含描述、正确表情和3个干扰项。
"""

EMOJI_MAPPINGS: list[dict] = [
    # ========== 情绪表情 (10 个) ==========
    {"description": "开心", "correct": "😊", "decoys": ["😢", "😡", "😴"]},
    {"description": "悲伤", "correct": "😢", "decoys": ["😊", "😎", "🤗"]},
    {"description": "生气", "correct": "😡", "decoys": ["😊", "😴", "😘"]},
    {"description": "哭泣", "correct": "😭", "decoys": ["😂", "😎", "😴"]},
    {"description": "大笑", "correct": "😂", "decoys": ["😢", "😡", "😴"]},
    {"description": "害羞", "correct": "😳", "decoys": ["😎", "😡", "😴"]},
    {"description": "惊讶", "correct": "😮", "decoys": ["😊", "😴", "😎"]},
    {"description": "睡觉", "correct": "😴", "decoys": ["😂", "😡", "😎"]},
    {"description": "害怕", "correct": "😱", "decoys": ["😊", "😎", "😘"]},
    {"description": "喜欢", "correct": "😍", "decoys": ["😡", "😢", "😴"]},
    # ========== 动物表情 (10 个) ==========
    {"description": "猫咪", "correct": "🐱", "decoys": ["🐶", "🐼", "🐷"]},
    {"description": "小狗", "correct": "🐶", "decoys": ["🐱", "🐭", "🐯"]},
    {"description": "熊猫", "correct": "🐼", "decoys": ["🐱", "🐶", "🐷"]},
    {"description": "小猪", "correct": "🐷", "decoys": ["🐱", "🐶", "🐼"]},
    {"description": "小鸡", "correct": "🐔", "decoys": ["🐶", "🐱", "🐷"]},
    {"description": "老虎", "correct": "🐯", "decoys": ["🐱", "🐶", "🐷"]},
    {"description": "兔子", "correct": "🐰", "decoys": ["🐱", "🐶", "🐷"]},
    {"description": "猴子", "correct": "🐵", "decoys": ["🐱", "🐶", "🐷"]},
    {"description": "鸟儿", "correct": "🐦", "decoys": ["🐱", "🐶", "🐷"]},
    {"description": "鱼儿", "correct": "🐟", "decoys": ["🐱", "🐶", "🐷"]},
    # ========== 食物表情 (10 个) ==========
    {"description": "苹果", "correct": "🍎", "decoys": ["🍌", "🍊", "🍇"]},
    {"description": "香蕉", "correct": "🍌", "decoys": ["🍎", "🍊", "🍇"]},
    {"description": "橙子", "correct": "🍊", "decoys": ["🍎", "🍌", "🍇"]},
    {"description": "西瓜", "correct": "🍉", "decoys": ["🍎", "🍌", "🍊"]},
    {"description": "草莓", "correct": "🍓", "decoys": ["🍎", "🍌", "🍊"]},
    {"description": "汉堡", "correct": "🍔", "decoys": ["🍕", "🍜", "🍰"]},
    {"description": "披萨", "correct": "🍕", "decoys": ["🍔", "🍜", "🍰"]},
    {"description": "面条", "correct": "🍜", "decoys": ["🍔", "🍕", "🍰"]},
    {"description": "蛋糕", "correct": "🍰", "decoys": ["🍔", "🍕", "🍜"]},
    {"description": "冰淇淋", "correct": "🍦", "decoys": ["🍔", "🍕", "🍜"]},
    # ========== 天气/自然 (10 个) ==========
    {"description": "太阳", "correct": "☀️", "decoys": ["🌙", "⭐", "☁️"]},
    {"description": "月亮", "correct": "🌙", "decoys": ["☀️", "⭐", "☁️"]},
    {"description": "星星", "correct": "⭐", "decoys": ["☀️", "🌙", "☁️"]},
    {"description": "云朵", "correct": "☁️", "decoys": ["☀️", "🌙", "⭐"]},
    {"description": "下雨", "correct": "🌧️", "decoys": ["☀️", "🌙", "⭐"]},
    {"description": "打雷", "correct": "⚡", "decoys": ["☀️", "🌙", "⭐"]},
    {"description": "下雪", "correct": "❄️", "decoys": ["☀️", "🌧️", "⚡"]},
    {"description": "彩虹", "correct": "🌈", "decoys": ["☀️", "🌙", "⭐"]},
    {"description": "树木", "correct": "🌲", "decoys": ["☀️", "🌙", "⭐"]},
    {"description": "花朵", "correct": "🌸", "decoys": ["🌲", "☀️", "🌙"]},
    # ========== 交通/物品 (10 个) ==========
    {"description": "汽车", "correct": "🚗", "decoys": ["🚕", "🚌", "🚲"]},
    {"description": "出租车", "correct": "🚕", "decoys": ["🚗", "🚌", "🚲"]},
    {"description": "公交车", "correct": "🚌", "decoys": ["🚗", "🚕", "🚲"]},
    {"description": "自行车", "correct": "🚲", "decoys": ["🚗", "🚕", "🚌"]},
    {"description": "飞机", "correct": "✈️", "decoys": ["🚗", "🚕", "🚲"]},
    {"description": "火车", "correct": "🚄", "decoys": ["🚗", "🚕", "🚲"]},
    {"description": "房子", "correct": "🏠", "decoys": ["🚗", "🚕", "🚲"]},
    {"description": "学校", "correct": "🏫", "decoys": ["🏠", "🚗", "🚕"]},
    {"description": "书本", "correct": "📚", "decoys": ["🚗", "🏠", "🚕"]},
    {"description": "电话", "correct": "📱", "decoys": ["📚", "🏠", "🚗"]},
]
