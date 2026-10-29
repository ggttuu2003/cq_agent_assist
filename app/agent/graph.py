"""最小对话图，后续在这里增加检索和工具节点。"""

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from openai import APIConnectionError, APIResponseValidationError, APIStatusError, APITimeoutError

from app.llm.errors import ModelError


class ModelNode:
    """模型节点：保存模型客户端，并在图运行时调用模型。"""

    def __init__(self, model: BaseChatModel):
        """创建节点时保存模型，后续通过 self.model 使用。"""
        self.model = model

    async def call_model(self, state: MessagesState) -> dict:
        """读取本次对话的消息，返回需要追加到状态中的模型回答。"""
        try:
            # ainvoke 是异步调用方法；await 等待模型回答，同时允许处理其他请求。
            response = await self.model.ainvoke(state["messages"])
        except APITimeoutError:
            raise ModelError("模型响应超时，请稍后重试") from None
        except APIStatusError as exc:
            raise ModelError(
                f"模型服务返回 HTTP {exc.status_code}，请检查模型配置或额度"
            ) from None
        except APIConnectionError:
            raise ModelError("无法连接模型服务，请检查网络和服务地址") from None
        except (APIResponseValidationError, ValueError, KeyError, IndexError, TypeError):
            raise ModelError("模型返回的数据格式不符合预期") from None

        if not response.text.strip():
            raise ModelError("模型没有返回有效的文本回答")
        # MessagesState 自动追加消息，不需要手动拼接整个列表。
        return {"messages": [response]}


def build_agent(model: BaseChatModel) -> CompiledStateGraph:
    """接收模型客户端，创建并返回一张可以执行的对话图。

    当前执行顺序：开始 → 调用模型 → 结束。
    BaseChatModel 是 LangChain 的模型基类，百炼等客户端都可以按这个接口使用。
    """

    # 把模型交给节点对象保存，不再通过嵌套函数引用外层变量。
    model_node = ModelNode(model)
    # MessagesState 是 LangGraph 自带的状态结构，其中的 messages 保存消息列表。
    builder = StateGraph(MessagesState)
    # 这里只登记对象的方法；图运行到 model 节点时，才会调用它。
    # model_node.call_model 已经绑定了对象，框架只需要传入 state。
    builder.add_node("model", model_node.call_model)
    # 边表示执行顺序；START 和 END 是框架提供的开始、结束标记。
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    # compile 把流程定义变成可执行的图；这里只创建流程，不会立即请求百炼。
    return builder.compile()
