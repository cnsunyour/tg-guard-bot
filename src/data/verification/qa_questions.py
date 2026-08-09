"""问答验证题库

包含常识问答题目，用于 Q&A 验证类型。
题面与选项文案走 catalog（verification.qa.bank.<id>.*），这里只保留稳定 id
与正确选项索引（0-3，对应 catalog 的 option_a/b/c/d）。
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QAQuestion:
    """问答题目：稳定 id（查 catalog 文案）+ 正确选项索引（0-3）"""

    id: str
    correct_index: int


QA_QUESTIONS: tuple[QAQuestion, ...] = (
    # ========== 常识知识 (10 题) ==========
    QAQuestion("months_in_year", 3),  # 一年有多少个月？→12个月
    QAQuestion("days_in_week", 2),  # 一周有多少天？→7天
    QAQuestion("sunrise_direction", 0),  # 太阳从哪个方向升起？→东方
    QAQuestion("earth_shape", 2),  # 地球是什么形状的？→球形
    QAQuestion("water_freezing_point", 0),  # 水在多少摄氏度时会结冰？→0度
    QAQuestion("rainbow_colors", 2),  # 彩虹有几种颜色？→7种
    QAQuestion("normal_body_temperature", 1),  # 人体正常体温大约是多少摄氏度？→37度
    QAQuestion("hours_in_day", 1),  # 一天有多少小时？→24小时
    QAQuestion("seawater_taste", 2),  # 海水是什么味道的？→咸的
    QAQuestion("leaf_color", 2),  # 树叶通常是什么颜色？→绿色
    # ========== 生活常识 (10 题) ==========
    QAQuestion("traffic_light_go_color", 2),  # 红绿灯中，哪个颜色表示可以通行？→绿色
    QAQuestion("phone_purpose", 1),  # 电话号码通常用来做什么？→打电话
    QAQuestion("rainy_day_item", 1),  # 下雨天出门应该带什么？→雨伞
    QAQuestion("before_sleep", 1),  # 睡觉前通常会做什么？→刷牙
    QAQuestion("supermarket_purpose", 1),  # 超市是用来做什么的？→购物
    QAQuestion("washing_hands_tool", 3),  # 洗手时通常使用哪种清洁用品？→肥皂
    QAQuestion("winter_clothing", 0),  # 寒冷的冬天出门应该穿什么？→羽绒服
    QAQuestion("sick_destination", 1),  # 身体不舒服需要看医生时，应该去哪里？→医院
    QAQuestion("refrigerator_purpose", 3),  # 冰箱的主要用途是什么？→冷藏保鲜食物
    QAQuestion("mailbox_purpose", 0),  # 信箱主要用来做什么？→收信
    # ========== 中国文化 (10 题) ==========
    QAQuestion("capital_of_china", 2),  # 中国的首都是哪里？→北京
    QAQuestion("spring_festival_meaning", 2),  # 春节是中国的什么节日？→新年
    QAQuestion("china_flag_color", 2),  # 中国的国旗是什么颜色？→红色
    QAQuestion("dumpling_category", 0),  # 饺子是中国的什么？→传统食物
    QAQuestion("china_currency", 1),  # 中国的货币单位是什么？→人民币
    QAQuestion("mid_autumn_food", 0),  # 中秋节人们通常吃什么？→月饼
    QAQuestion("chopsticks", 3),  # 在中国，人们传统上使用哪种餐具吃饭？→筷子
    QAQuestion("great_wall", 0),  # 长城属于哪类古代工程？→古代防御工程
    QAQuestion("tea_origin", 3),  # 世界上最早发现并利用茶的国家是？→中国
    QAQuestion("peking_opera", 0),  # 京剧属于哪种艺术形式？→中国传统戏曲
    # ========== 动物常识 (10 题) ==========
    QAQuestion("dog_human_relationship", 1),  # 狗是人类的什么？→朋友
    QAQuestion("fish_habitat", 2),  # 鱼生活在哪里？→水里
    QAQuestion("cat_favorite_food", 1),  # 猫最喜欢吃什么？→鱼
    QAQuestion("dairy_cow_product", 0),  # 奶牛主要为人们提供什么？→牛奶
    QAQuestion("bird_fly_tool", 3),  # 大多数鸟主要用哪个身体部位飞行？→翅膀
    QAQuestion("bee_produce", 3),  # 蜜蜂能酿造什么？→蜂蜜
    QAQuestion("rabbit_food", 3),  # 宠物兔的主要食物是什么？→干草
    QAQuestion("hen_lay", 0),  # 母鸡能产下什么？→蛋
    QAQuestion("elephant_trunk_use", 3),  # 大象主要用哪个身体部位吸水和取食？→长鼻子
    QAQuestion("emperor_penguin_habitat", 3),  # 帝企鹅主要生活在哪里？→南极
)
