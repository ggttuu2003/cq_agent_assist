"""模型配置和调用异常。"""


class ModelError(Exception):
    """表示模型调用超时、连接失败或回答异常，接口将其转换成 HTTP 502。"""


class ModelConfigurationError(Exception):
    """表示模型地址、密钥等配置有误，接口将其转换成 HTTP 503。"""
