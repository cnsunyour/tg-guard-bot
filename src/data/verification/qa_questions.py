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
    QAQuestion("months_in_year", 2),  # 一年有多少个月？→12个月
    QAQuestion("days_in_week", 2),  # 一周有多少天？→7天
    QAQuestion("sunrise_direction", 2),  # 太阳从哪个方向升起？→东方
    QAQuestion("earth_shape", 2),  # 地球是什么形状的？→球形
    QAQuestion("water_freezing_point", 0),  # 水在多少摄氏度时会结冰？→0度
    QAQuestion("rainbow_colors", 2),  # 彩虹有几种颜色？→7种
    QAQuestion("normal_body_temperature", 1),  # 人体正常体温大约是多少摄氏度？→37度
    QAQuestion("hours_in_day", 1),  # 一天有多少小时？→24小时
    QAQuestion("seawater_taste", 2),  # 海水是什么味道的？→咸的
    QAQuestion("leaf_color", 2),  # 树叶通常是什么颜色？→绿色
    # ========== 简单数学 (5 题) ==========
    QAQuestion("five_plus_five", 2),  # 5 + 5 等于多少？→10
    QAQuestion("ten_minus_three", 2),  # 10 - 3 等于多少？→7
    QAQuestion("two_times_four", 2),  # 2 × 4 等于多少？→8
    QAQuestion("fifteen_divided_by_three", 2),  # 15 ÷ 3 等于多少？→5
    QAQuestion("largest_number", 1),  # 哪个数字最大？→28
    # ========== 生活常识 (5 题) ==========
    QAQuestion("traffic_light_go_color", 2),  # 红绿灯中，哪个颜色表示可以通行？→绿色
    QAQuestion("phone_purpose", 1),  # 电话号码通常用来做什么？→打电话
    QAQuestion("rainy_day_item", 1),  # 下雨天出门应该带什么？→雨伞
    QAQuestion("before_sleep", 1),  # 睡觉前通常会做什么？→刷牙
    QAQuestion("supermarket_purpose", 1),  # 超市是用来做什么的？→购物
    # ========== 中国文化 (5 题) ==========
    QAQuestion("capital_of_china", 2),  # 中国的首都是哪里？→北京
    QAQuestion("spring_festival_meaning", 2),  # 春节是中国的什么节日？→新年
    QAQuestion("china_flag_color", 2),  # 中国的国旗是什么颜色？→红色
    QAQuestion("dumpling_category", 0),  # 饺子是中国的什么？→传统食物
    QAQuestion("china_currency", 2),  # 中国的货币单位是什么？→人民币
    # ========== 动物常识 (3 题) ==========
    QAQuestion("dog_human_relationship", 1),  # 狗是人类的什么？→朋友
    QAQuestion("fish_habitat", 2),  # 鱼生活在哪里？→水里
    QAQuestion("cat_favorite_food", 1),  # 猫最喜欢吃什么？→鱼
)
