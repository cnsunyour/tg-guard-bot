"""模型训练脚本 - 离线训练反垃圾模型"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from src.core.database import init_db
from src.repositories.spam_repo import SpamRepository
from src.ml.classifier import get_classifier


async def train_model_from_db():
    """从数据库加载样本并训练模型"""
    logger.info("开始模型训练流程...")

    # 初始化数据库
    await init_db()
    logger.info("数据库已初始化")

    # 获取训练数据
    logger.info("正在加载训练数据...")
    texts, labels = await SpamRepository.get_training_data()

    if len(texts) < 10:
        logger.error(f"训练样本不足: {len(texts)}，至少需要 10 个样本")
        return False

    logger.info(f"加载了 {len(texts)} 个训练样本")
    logger.info(f"- 垃圾样本: {sum(labels)}")
    logger.info(f"- 正常样本: {len(labels) - sum(labels)}")

    # 训练模型
    logger.info("开始训练模型...")
    classifier = get_classifier()

    try:
        accuracy, metrics = classifier.train(texts, labels)

        logger.info("=" * 50)
        logger.info("训练完成！")
        logger.info(f"准确率: {accuracy:.2%}")
        logger.info(f"总样本数: {metrics['total_samples']}")
        logger.info(f"垃圾样本: {metrics['spam_samples']}")
        logger.info(f"正常样本: {metrics['normal_samples']}")
        logger.info("=" * 50)

        # 保存模型
        logger.info("正在保存模型...")
        if classifier.save_model():
            logger.info(f"✅ 模型已保存到: {classifier.model_path}")
        else:
            logger.error("❌ 模型保存失败")
            return False

        return True

    except Exception as e:
        logger.error(f"训练失败: {e}")
        return False


async def add_sample_data():
    """添加一些示例数据（用于测试）"""
    logger.info("添加示例数据...")

    # 初始化数据库
    await init_db()

    # 垃圾样本
    spam_samples = [
        "加微信免费领取礼品，先到先得",
        "点击链接下载APP，注册送现金",
        "兼职刷单日赚500，零投资高回报",
        "美女上门服务，价格优惠",
        "赌博网站充值送钱，提现秒到账",
        "私聊我获取内部消息，稳赚不赔",
        "扫码进群领福利，手慢无",
        "投资理财高收益，月入过万",
        "加V信：abc123，免费咨询",
        "点击 t.me/+abc123 加入频道",
    ]

    # 正常样本
    normal_samples = [
        "大家好，我是新来的",
        "这个问题怎么解决？",
        "谢谢大家的帮助",
        "有人知道这个功能怎么用吗",
        "今天天气不错",
        "周末有什么活动吗",
        "这个项目很有意思",
        "可以分享一下经验吗",
        "我也遇到过类似的问题",
        "感谢分享！",
    ]

    # 添加样本
    for text in spam_samples:
        await SpamRepository.add_sample(text=text, is_spam=True, labeled_by=0)

    for text in normal_samples:
        await SpamRepository.add_sample(text=text, is_spam=False, labeled_by=0)

    logger.info(f"已添加 {len(spam_samples)} 个垃圾样本")
    logger.info(f"已添加 {len(normal_samples)} 个正常样本")


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="反垃圾模型训练脚本")
    parser.add_argument(
        "--add-samples", action="store_true", help="添加示例数据（用于测试）"
    )
    parser.add_argument("--train", action="store_true", help="训练模型")

    args = parser.parse_args()

    if args.add_samples:
        asyncio.run(add_sample_data())

    if args.train or not args.add_samples:
        success = asyncio.run(train_model_from_db())
        if success:
            logger.info("✅ 训练流程完成")
        else:
            logger.error("❌ 训练流程失败")
            sys.exit(1)


if __name__ == "__main__":
    main()
