from typing import Any, Callable, Optional, Union

from openai.types.beta.threads import Message
from openai.types.beta.threads.run import Run as OpenAIRun
from openai.types.beta.threads.runs import RunStep as OpenAIRunStep
from pydantic import BaseModel, Field, PrivateAttr, field_validator

import marvin.utilities.openai
import marvin.utilities.tools
from marvin.beta.assistants.handlers import AsyncRunHandler, PrintRunHandler, RunHandler
from marvin.tools.assistants import AssistantTool, CancelRun
from marvin.types import Tool
from marvin.utilities.asyncio import ExposeSyncMethodsMixin, expose_sync_method
from marvin.utilities.logging import get_logger

from .assistants import Assistant
from .threads import Thread

logger = get_logger("Runs")


class Run(BaseModel, ExposeSyncMethodsMixin):
    """
    The Run class represents a single execution of an assistant.

    Attributes:
        thread (Thread): The thread in which the run is executed.
        assistant (Assistant): The assistant that is being run.
        instructions (str, optional): Replacement instructions for the run.
        additional_instructions (str, optional): Additional instructions to append
                                                 to the assistant's instructions.
        tools (list[Union[AssistantTool, Callable]], optional): Replacement tools
                                                               for the run.
        additional_tools (list[AssistantTool], optional): Additional tools to append
                                                          to the assistant's tools.
        run (OpenAIRun): The OpenAI run object.
        data (Any): Any additional data associated with the run.
    """

    model_config: dict = dict(extra="forbid")

    thread: Thread
    assistant: Assistant
    event_handler_class: type[Union[RunHandler, AsyncRunHandler]] = Field(
        default=PrintRunHandler
    )
    event_handler_kwargs: dict[str, Any] = Field(default={})
    _messages: list[Message] = PrivateAttr({})
    _steps: list[OpenAIRunStep] = PrivateAttr({})
    instructions: Optional[str] = Field(
        None, description="Replacement instructions to use for the run."
    )
    additional_instructions: Optional[str] = Field(
        None,
        description=(
            "Additional instructions to append to the assistant's instructions."
        ),
    )
    tools: Optional[list[Union[AssistantTool, Callable]]] = Field(
        None, description="Replacement tools to use for the run."
    )
    additional_tools: Optional[list[AssistantTool]] = Field(
        None,
        description="Additional tools to append to the assistant's tools. ",
    )
    run: OpenAIRun = None
    data: Any = None

    def __init__(self, *, messages: list[Message] = None, **data):
        super().__init__(**data)
        if messages is not None:
            self._messages.update({m.id: m for m in messages})

    @field_validator("tools", "additional_tools", mode="before")
    def format_tools(cls, tools: Union[None, list[Union[Tool, Callable]]]):
        if tools is not None:
            return [
                (
                    tool
                    if isinstance(tool, Tool)
                    else marvin.utilities.tools.tool_from_function(tool)
                )
                for tool in tools
            ]

    @field_validator("event_handler_class", mode="before")
    def no_event_handler(
        cls, event_handler_class: type[Union[RunHandler, AsyncRunHandler]]
    ):
        # the default event handler is a PrintRunHandler but if None is passed,
        # we use a no-op handler
        if event_handler_class is None:
            return AsyncRunHandler
        return event_handler_class

    @property
    def messages(self) -> list[Message]:
        return sorted(self._messages.values(), key=lambda m: m.created_at)

    @property
    def steps(self) -> list[OpenAIRunStep]:
        return sorted(self._steps.values(), key=lambda s: s.created_at)

    @expose_sync_method("refresh")
    async def refresh_async(self):
        """Refreshes the run."""
        if not self.run:
            raise ValueError("Run has not been created yet.")
        client = marvin.utilities.openai.get_openai_client()
        self.run = await client.beta.threads.runs.retrieve(
            run_id=self.run.id, thread_id=self.thread.id
        )

    @expose_sync_method("cancel")
    async def cancel_async(self):
        """Cancels the run."""
        if not self.run:
            raise ValueError("Run has not been created yet.")
        client = marvin.utilities.openai.get_openai_client()
        await client.beta.threads.runs.cancel(
            run_id=self.run.id, thread_id=self.thread.id
        )
        await self.refresh_async()

    def get_instructions(self) -> str:
        if self.instructions is None:
            instructions = self.assistant.get_instructions() or ""
        else:
            instructions = self.instructions

        if self.additional_instructions is not None:
            instructions = "\n\n".join([instructions, self.additional_instructions])

        return instructions

    def get_tools(self) -> list[AssistantTool]:
        tools = []
        if self.tools is None:
            tools.extend(self.assistant.get_tools())
        else:
            tools.extend(self.tools)
        if self.additional_tools is not None:
            tools.extend(self.additional_tools)
        return tools

    async def get_tool_outputs(self, run: OpenAIRun) -> list[dict[str, str]]:
        if run.status != "requires_action":
            return None, None
        if run.required_action.type == "submit_tool_outputs":
            tool_calls = []
            tool_outputs = []
            tools = self.get_tools()

            for tool_call in run.required_action.submit_tool_outputs.tool_calls:
                try:
                    output = marvin.utilities.tools.call_function_tool(
                        tools=tools,
                        function_name=tool_call.function.name,
                        function_arguments_json=tool_call.function.arguments,
                        return_string=True,
                    )
                except CancelRun as exc:
                    logger.debug(f"Ending run with data: {exc.data}")
                    raise
                except Exception as exc:
                    output = f"Error calling function {tool_call.function.name}: {exc}"
                    logger.error(output)
                tool_outputs.append(
                    dict(tool_call_id=tool_call.id, output=output or "")
                )
                tool_calls.append(tool_call)

            return tool_outputs

    async def run_async(self):
        client = marvin.utilities.openai.get_openai_client()

        run_kwargs = {}
        if self.instructions is not None or self.additional_instructions is not None:
            run_kwargs["instructions"] = self.get_instructions()

        if self.tools is not None or self.additional_tools is not None:
            run_kwargs["tools"] = self.get_tools()

        if self.run is not None:
            raise ValueError(
                "This run object was provided an ID; can not create a new run."
            )
        with self.assistant:
            handler = self.event_handler_class(**self.event_handler_kwargs)

            try:
                self.assistant.pre_run_hook()

                for msg in self.messages:
                    await handler.on_message_done(msg)

                async with client.beta.threads.runs.create_and_stream(
                    thread_id=self.thread.id,
                    assistant_id=self.assistant.id,
                    event_handler=handler,
                    **run_kwargs,
                ) as stream:
                    await stream.until_done()
                    self.run = handler.current_run
                    self._messages.update(
                        {m.id: m for m in await handler.get_final_messages()}
                    )
                    self._steps.update(
                        {s.id: s for s in await handler.get_final_run_steps()}
                    )

                while handler.current_run.status in ["requires_action"]:
                    tool_outputs = await self.get_tool_outputs(run=handler.current_run)
                    handler = self.event_handler_class(**self.event_handler_kwargs)
                    async with client.beta.threads.runs.submit_tool_outputs_stream(
                        thread_id=self.thread.id,
                        run_id=self.run.id,
                        tool_outputs=tool_outputs,
                        event_handler=handler,
                    ) as stream:
                        await stream.until_done()
                        self.run = handler.current_run
                        self._messages.update(
                            {m.id: m for m in await handler.get_final_messages()}
                        )
                        self._steps.update(
                            {s.id: s for s in await handler.get_final_run_steps()}
                        )

            except CancelRun as exc:
                logger.debug(f"`CancelRun` raised; ending run with data: {exc.data}")
                await self.cancel_async()
                self.data = exc.data

            except Exception as exc:
                await handler.on_exception(exc)
                raise

            if self.run.status == "failed":
                logger.debug(
                    f"Run failed. Last error was: {handler.current_run.last_error}"
                )

            self.assistant.post_run_hook(run=self)

        return self
