"""表情验证映射表

包含文字描述到表情符号的映射，用于 Emoji 验证类型。
描述文案走 catalog（verification.emoji.bank.<id>.description），这里只保留
稳定 id、正确表情与干扰项（表情符号本身不翻译）。
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmojiMapping:
    """表情映射：稳定 id（查 catalog 描述）+ 正确表情 + 干扰项"""

    id: str
    correct: str
    decoys: tuple[str, ...]


EMOJI_MAPPINGS: tuple[EmojiMapping, ...] = (
    # ========== 情绪表情 (10 个) ==========
    EmojiMapping("happy", "😊", ("😢", "😡", "😴")),
    EmojiMapping("sad", "😢", ("😊", "😎", "🤗")),
    EmojiMapping("angry", "😡", ("😊", "😴", "😘")),
    EmojiMapping("crying", "😭", ("😂", "😎", "😴")),
    EmojiMapping("laughing", "😂", ("😢", "😡", "😴")),
    EmojiMapping("shy", "😳", ("😎", "😡", "😴")),
    EmojiMapping("surprised", "😮", ("😊", "😴", "😎")),
    EmojiMapping("sleeping", "😴", ("😂", "😡", "😎")),
    EmojiMapping("scared", "😱", ("😊", "😎", "😘")),
    EmojiMapping("love", "😍", ("😡", "😢", "😴")),
    # ========== 动物表情 (10 个) ==========
    EmojiMapping("cat", "🐱", ("🐶", "🐼", "🐷")),
    EmojiMapping("dog", "🐶", ("🐱", "🐭", "🐯")),
    EmojiMapping("panda", "🐼", ("🐱", "🐶", "🐷")),
    EmojiMapping("pig", "🐷", ("🐱", "🐶", "🐼")),
    EmojiMapping("chicken", "🐔", ("🐶", "🐱", "🐷")),
    EmojiMapping("tiger", "🐯", ("🐱", "🐶", "🐷")),
    EmojiMapping("rabbit", "🐰", ("🐱", "🐶", "🐷")),
    EmojiMapping("monkey", "🐵", ("🐱", "🐶", "🐷")),
    EmojiMapping("bird", "🐦", ("🐱", "🐶", "🐷")),
    EmojiMapping("fish", "🐟", ("🐱", "🐶", "🐷")),
    # ========== 食物表情 (10 个) ==========
    EmojiMapping("apple", "🍎", ("🍌", "🍊", "🍇")),
    EmojiMapping("banana", "🍌", ("🍎", "🍊", "🍇")),
    EmojiMapping("orange", "🍊", ("🍎", "🍌", "🍇")),
    EmojiMapping("watermelon", "🍉", ("🍎", "🍌", "🍊")),
    EmojiMapping("strawberry", "🍓", ("🍎", "🍌", "🍊")),
    EmojiMapping("hamburger", "🍔", ("🍕", "🍜", "🍰")),
    EmojiMapping("pizza", "🍕", ("🍔", "🍜", "🍰")),
    EmojiMapping("noodles", "🍜", ("🍔", "🍕", "🍰")),
    EmojiMapping("cake", "🍰", ("🍔", "🍕", "🍜")),
    EmojiMapping("ice_cream", "🍦", ("🍔", "🍕", "🍜")),
    # ========== 天气/自然 (10 个) ==========
    EmojiMapping("sun", "☀️", ("🌙", "⭐", "☁️")),
    EmojiMapping("moon", "🌙", ("☀️", "⭐", "☁️")),
    EmojiMapping("star", "⭐", ("☀️", "🌙", "☁️")),
    EmojiMapping("cloud", "☁️", ("☀️", "🌙", "⭐")),
    EmojiMapping("rain", "🌧️", ("☀️", "🌙", "⭐")),
    EmojiMapping("thunder", "⚡", ("☀️", "🌙", "⭐")),
    EmojiMapping("snow", "❄️", ("☀️", "🌧️", "⚡")),
    EmojiMapping("rainbow", "🌈", ("☀️", "🌙", "⭐")),
    EmojiMapping("tree", "🌲", ("☀️", "🌙", "⭐")),
    EmojiMapping("flower", "🌸", ("🌲", "☀️", "🌙")),
    # ========== 交通/物品 (10 个) ==========
    EmojiMapping("car", "🚗", ("🚕", "🚌", "🚲")),
    EmojiMapping("taxi", "🚕", ("🚗", "🚌", "🚲")),
    EmojiMapping("bus", "🚌", ("🚗", "🚕", "🚲")),
    EmojiMapping("bicycle", "🚲", ("🚗", "🚕", "🚌")),
    EmojiMapping("airplane", "✈️", ("🚗", "🚕", "🚲")),
    EmojiMapping("train", "🚄", ("🚗", "🚕", "🚲")),
    EmojiMapping("house", "🏠", ("🚗", "🚕", "🚲")),
    EmojiMapping("school", "🏫", ("🏠", "🚗", "🚕")),
    EmojiMapping("book", "📚", ("🚗", "🏠", "🚕")),
    EmojiMapping("phone", "📱", ("📚", "🏠", "🚗")),
)
