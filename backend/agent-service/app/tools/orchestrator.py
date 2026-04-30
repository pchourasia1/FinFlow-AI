import os
import sys
import json
import asyncio
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from typing import TypedDict, Annotated, List, Dict, Any, Optional, Tuple

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

sys.path.append(str(Path(__file__).parent.parent.parent))
from app.tools.rag_tool import rag_tool
from app.tools.sql_analytics_tool import sql_analytics_tool
from app.tools.balance_query_tool import balance_tool
from app.utils.category_normalizer import validate_category


env_path = Path(__file__).parent.parent.parent.parent / '.env'
load_dotenv()

# Override in backend/.env with GEMINI_MODEL
DEFAULT_MODEL = 'gemini-3.5-flash-lite'


def _today() -> str:
    """Today's date for the LLM, or FINFLOW_AS_OF_DATE when simulating the date the data was generated for"""
    return os.getenv('FINFLOW_AS_OF_DATE') or datetime.now().strftime('%Y-%m-%d')


# Load orchestration prompts
with open('app/llm/prompts/orchestration.json', 'r') as f:
    orchestration_prompts = json.load(f)
    RAG_PROMPT = orchestration_prompts['rag_prompt']
    SPENDING_PROMPT = orchestration_prompts['spending_prompt']
    BALANCE_PROMPT = orchestration_prompts['balance_prompt']


def _message_text(message) -> str:
    """Return a message's text; Gemini 3.x returns content as a list of blocks, not a string"""
    content = getattr(message, 'content', None)
    if not content:
        return ""
    if isinstance(content, str):
        return content

    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


# ---------- Tools ----------
# Each tool returns (text for the LLM, raw data for the "How I got this" panel).
# With response_format="content_and_artifact", LangChain puts the raw data on
# ToolMessage.artifact, so each service is called once per tool call.

def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None


@tool("search_policy_documents", description=RAG_PROMPT, response_format="content_and_artifact")
async def search_policy_documents(query: str) -> Tuple[str, List[Dict[str, Any]]]:
    results = (await rag_tool.search_policy(query)).get("results", [])
    if not results:
        return "No relevant policy information found.", []

    # Same format as the vectorstore's /search/rag-context
    context = "\n---\n".join(f"**{r['title']}**\n{r['content']}\n" for r in results)
    return context, results


@tool("analyze_spending", description=SPENDING_PROMPT, response_format="content_and_artifact")
async def analyze_spending(
    user_id: int,
    category: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    group_by: str = "category",
    account_id: Optional[int] = None,
    account_type: Optional[str] = None
) -> Tuple[str, Dict[str, Any]]:

    # Normalize category before calling the service
    normalized_category = None
    if category:
        _, normalized_category = validate_category(category)

    result = await sql_analytics_tool.analyze_spending(
        user_id = user_id,
        category = normalized_category,
        start_date = _parse_date(start_date),
        end_date = _parse_date(end_date),
        group_by = group_by,
        account_id = account_id,
        account_type = account_type
    )

    summary = result.get('summary', {})
    results = result.get('results', [])

    response = f"Spending Analysis:\n"
    response += f"Total Spending: ${summary.get('total_spending', 0):.2f}\n"
    response += f"Total Transactions: {summary.get('total_transactions', 0)}\n\n"

    if results and group_by == "month":
        # The API sorts by amount; list months in date order with readable labels
        # ("July 2026", not "2026-07-01T00:00:00") so the LLM doesn't mislabel them
        response += "Breakdown by month (chronological):\n"
        for item in sorted(results, key=lambda r: str(r.get('month', ''))):
            try:
                label = datetime.fromisoformat(str(item.get('month'))).strftime('%B %Y')
            except ValueError:
                label = item.get('month', 'N/A')
            response += (
                f"  {label}: ${item.get('total_spending', 0):.2f} "
                f"({item.get('transaction_count', 0)} transactions)\n"
            )
    elif results:
        response += "Breakdown:\n"
        for item in results[:10]:  # Limit to top 10
            if group_by == "category":
                response += f"  {item.get('group_value', 'N/A')}: ${item.get('total_spending', 0):.2f}\n"
            else:
                response += f"  {item}: ${item.get('total_spending', 0):.2f}\n"

    return response, result


@tool("get_account_balances", description=BALANCE_PROMPT, response_format="content_and_artifact")
async def get_account_balances(
    user_id: int,
    account_id: Optional[int] = None,
    account_type: Optional[str] = None
) -> Tuple[str, Dict[str, Any]]:

    result = await balance_tool.get_balance(
        user_id = user_id,
        account_id = account_id,
        account_type = account_type
    )

    accounts = result.get('results', [])
    if not accounts:
        return "No accounts found.", result

    response = "Account Balances:\n"

    total = 0
    for account in accounts:
        acc_type = account.get('type', 'N/A')
        acc_id = account.get('account_id', 'N/A')
        balance = account.get('balance', 0)
        total += balance
        response += f"  {acc_type.capitalize()} Account #{acc_id}: ${balance:,.2f}\n"

    response += f"\nTotal Balance: ${total:,.2f}\n"

    return response, result


TOOLS = [search_policy_documents, analyze_spending, get_account_balances]


class AgentState(TypedDict):
    """State for the agent"""
    messages: Annotated[List, lambda x, y: x + y] # Accumulates messages
    user_id: int
    query: str
    tool_results: Dict[str, Any]    # Store results from tool calls
    tool_metadata: List[Dict[str, Any]]    # Tool args / RAG hits / balances streamed to the frontend



class Orchestrator:
    """
    Main orchestrator that:
    1. Receives user query
    2. Uses LLM to determine the intent
    3. Selects appropriate tool(s) based on the intent
    4. Calls tool with extracted parameters
    5. Formats response
    """

    def __init__(self, model: str = None):

        if model is None:
            self.model = os.getenv('GEMINI_MODEL') or DEFAULT_MODEL
        else:
            self.model = model

        # No temperature: Gemini 3.x uses fixed sampling defaults and ignores it
        self.llm = ChatGoogleGenerativeAI(
            model = self.model,
            streaming = True,
            api_key = os.getenv('GOOGLE_API_KEY'),
        )

        self.tools = TOOLS
        self.llm_with_tools = self.llm.bind_tools(self.tools)
        self.memory = MemorySaver()
        self.graph = self._build_graph()
        self.app = self.graph.compile(checkpointer = self.memory)

    def _build_graph(self) -> StateGraph:
        """Build LangGraph state machine"""

        graph = StateGraph(AgentState)

        # Add nodes
        graph.add_node("router", self._route_query)
        graph.add_node("tools", self._execute_tools)
        graph.add_node("respond", self._format_response)

        # Add edges
        graph.add_edge(START, "router")

        # Conditional edge: after routing, check if tools need to be called
        graph.add_conditional_edges(
            "router",
            self._should_call_tools,
            {
                "tools": "tools",
                "respond": "respond"
            }
        )
        
        # After tools execute, alwaus format response
        graph.add_edge("tools", "respond")
        graph.add_edge("respond", END)

        return graph

    
    async def _execute_tools(self, state: AgentState) -> AgentState:
        """Execute tools asynchronously"""

        messages = state.get("messages", [])
        last_message = messages[-1] if messages else None

        if not last_message or not hasattr(last_message, 'tool_calls') or not last_message.tool_calls:
            return {"messages": []}

        
        # DEBUG: Log tool calls
        print("\n" + "="*80)
        print("EXECUTE TOOLS - TOOL CALLS:")
        for tc in last_message.tool_calls:
            print(f"  - {tc.get('name')}: {tc.get('args', {})}")
        print("="*80)

        # Store tool metadate for frontend display
        tool_metadata = []

        # Helper function to execute a single tool call
        async def execute_single_tool(tool_call):
            tool_name = tool_call.get("name")
            tool_args = tool_call.get("args", {})
            tool_call_id = tool_call.get("id")
            
            # Tools that require user_id
            tools_requiring_user_id = ['analyze_spending', 'get_account_balances']
            
            # Inject user_id from state if tool needs it and it's not in args
            if tool_name in tools_requiring_user_id and 'user_id' not in tool_args:
                state_user_id = state.get('user_id')
                if state_user_id:
                    tool_args['user_id'] = state_user_id
            
            # Remove user_id from tools that don't need it (like RAG tool)
            if tool_name not in tools_requiring_user_id and 'user_id' in tool_args:
                tool_args.pop('user_id', None)

            tool = next((t for t in self.tools if t.name == tool_name), None)
            if not tool:
                return ToolMessage(
                    content=f"Error: Tool '{tool_name}' not found.",
                    tool_call_id=tool_call_id
                )

            try:
                # Invoking with a full tool call returns a ToolMessage whose .artifact
                # holds the tool's raw data, so nothing is fetched twice
                message = await tool.ainvoke({
                    "type": "tool_call", "name": tool_name, "args": tool_args, "id": tool_call_id
                })

                # Metadata for the frontend's "How I got this" panel
                metadata = {"tool_name": tool_name, "tool_args": tool_args}
                if tool_name == "analyze_spending":
                    metadata["sql_params"] = tool_args
                elif tool_name == "search_policy_documents":
                    metadata["rag_results"] = (message.artifact or [])[:3]    # Top 3
                elif tool_name == "get_account_balances":
                    metadata["balance_results"] = message.artifact

                tool_metadata.append(metadata)
                return message
            except Exception as e:
                # Log the full error for debugging
                import traceback
                error_trace = traceback.format_exc()
                print(f"ERROR in {tool_name}: {str(e)}")
                print(f"Tool args: {tool_args}")
                print(f"Traceback: {error_trace}")
                
                return ToolMessage(
                    content=f"Error executing {tool_name}: {str(e)}",
                    tool_call_id=tool_call_id
                )

        # Execute all tool calls in parallel
        tool_messages = await asyncio.gather(*[
            execute_single_tool(tool_call)
            for tool_call in last_message.tool_calls
        ])
        
        # Store metadata in state for later retrieval
        state["tool_metadata"] = tool_metadata

        return {"messages": list(tool_messages), "tool_metadata": tool_metadata}
                


    def _system_prompt(self, user_id: int) -> str:
        """System context: the user's ID and today's date (so "last month" resolves correctly)"""
        return (
            f"The user's ID is {user_id}. Use this user_id when calling tools that require it "
            f"(like get_account_balances or analyze_spending). "
            f"Today's date is {_today()}; interpret relative dates such as \"last month\" from it."
        )

    async def _route_query(self, state: AgentState) -> AgentState:
        """Route the query using LLM to determine which tools to call"""

        messages = state.get("messages", [])
        user_id = state.get("user_id")

        # Check if we need to add system message (only if it's not already there)
        has_system_message = any(isinstance(msg, SystemMessage) for msg in messages)
        
        if not messages:
            # Build messages with system context about user_id
            system_messages = []
            if user_id:
                # Add system message to inform LLM about available user_id
                system_messages.append(SystemMessage(content=self._system_prompt(user_id)))
            
            # Add the user's query
            messages = system_messages + [HumanMessage(content=state["query"])]
        elif not has_system_message and user_id:
            # Prepend system message if it doesn't exist and we have user_id
            system_msg = SystemMessage(content=self._system_prompt(user_id))
            messages = [system_msg] + messages
        
        # Get LLM response with potential tool calls
        response = await self.llm_with_tools.ainvoke(messages)

        # DEBUG: Log LLM response
        print("\n" + "="*80)
        print("ORCHESTRATOR - LLM RESPONSE:")
        print(f"  Content: {response.content[:500] if hasattr(response, 'content') and response.content else 'None'}")
        if hasattr(response, 'tool_calls') and response.tool_calls:
            print(f"  Tool Calls: {len(response.tool_calls)}")
            for tc in response.tool_calls:
                print(f"    - {tc.get('name')}: {tc.get('args', {})}")
        else:
            print("  No tool calls")
        print("="*80 + "\n")

        return {
            "messages": [response],
            "tool_results": {}
        }

    
    def _should_call_tools(self, state: AgentState) -> str:
        """Determine if tools need to be called"""
        messages = state.get("messages", [])
        if not messages:
            return "respond"
        
        last_message = messages[-1]
        
        # Check if the last message has tool calls
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            return "tools"
        
        return "respond"

    async def _format_response(self, state: AgentState) -> AgentState:
        """Format the final response after tool execution"""

        messages = state.get("messages", [])

        # DEBUG: Log all messages before formatting
        # print("\n" + "="*80)
        # print("📝 _format_response - ALL MESSAGES IN STATE:")
        # for i, msg in enumerate(messages):
        #     msg_type = type(msg).__name__
        #     if isinstance(msg, ToolMessage):
        #         content_preview = str(msg.content)[:200]
        #         print(f"  [{i}] {msg_type}: {content_preview}...")
        #     elif hasattr(msg, 'content'):
        #         print(f"  [{i}] {msg_type}: {msg.content[:200]}")
        #     elif hasattr(msg, 'tool_calls'):
        #         print(f"  [{i}] {msg_type}: tool_calls={len(msg.tool_calls) if msg.tool_calls else 0}")
        #     else:
        #         print(f"  [{i}] {msg_type}: {str(msg)[:200]}")
        # print("="*80)

        # Get the last AI message
        tool_calls = []
        for msg in reversed(messages):
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                tool_calls = msg.tool_calls
                break

        # If there were tool calls, format response using all messages (which already include ToolMessages)
        if tool_calls:
            # Messages already contain the ToolMessages from _execute_tools
            # Add a system message to instruct the LLM to format a natural language response
            format_prompt = SystemMessage(
                content="Based on the tool results provided, format a clear and concise natural language response to the user's question. Do not make additional tool calls."
            )
            
            # Construct the conversation: original messages + format instruction
            response_messages = messages + [format_prompt]


            follow_up = HumanMessage(
                content="Please provide a clear answer to my question based on the tool results above."
            )
            response_messages = messages + [follow_up]
            
            final_response = await self.llm.ainvoke(response_messages)
            
            # DEBUG: Log final LLM response
            print("\n" + "="*80)
            print("✅ FORMAT RESPONSE - FINAL LLM RESPONSE:")
            print(f"  Content: {final_response.content if hasattr(final_response, 'content') else 'None'}")
            print(f"  Content length: {len(final_response.content) if hasattr(final_response, 'content') and final_response.content else 0}")
            if hasattr(final_response, 'tool_calls') and final_response.tool_calls:
                print(f"  WARNING: Response has tool_calls: {final_response.tool_calls}")
            print("="*80 + "\n")
        else:
            # No tool calls, just return the last message
            final_response = messages[-1] if messages else None
            print("\n" + "="*80)
            print("⚠️  _format_response - NO TOOL CALLS, RETURNING LAST MESSAGE")
            print("="*80 + "\n")

        return {
            "messages": [final_response] if final_response else [],
        }

    
    async def process_query(
        self,
        query: str,
        user_id: int,
        thread_id: Optional[str] = None,
        config: Optional[Dict] = None
    ):
        """Process a user query with streaming response"""

        # Create initial state
        initial_state = {
            "messages": [HumanMessage(content = query)],
            "user_id": user_id,
            "query": query,
            "tool_results": {}
        }

        # Configure for streaming response
        if config is None:
            config = {
                "configurable": {
                    "thread_id": thread_id or f"user_{user_id}_{datetime.now().timestamp()}"
                }
            }

        # Stream the graph execution
        async for event in self.app.astream(initial_state, config = config):
            # Yield events as they happen
            for node_name, node_output in event.items():
                # Check if node_output is None -> Avoid attribute errors later
                if node_output is None:
                    continue

                if node_name == "router":
                    # LLM thinking/routing
                    messages = node_output.get("messages", [])

                    if messages:
                        last_msg = messages[-1]

                        thinking = _message_text(last_msg)
                        if thinking:
                            yield {
                                "type": "thinking",
                                "content": thinking
                            }
                        
                        if hasattr(last_msg, 'tool_calls') and last_msg.tool_calls:
                            yield {
                                "type": "tool_call",
                                "tools": [tc.get("name") for tc in last_msg.tool_calls]
                            }
                
                elif node_name == "tools":
                    # Tools are executing - include metadata
                    tool_metadata = node_output.get("tool_metadata", [])
                    
                    yield {
                        "type": "tool_execution",
                        "status": "executing",
                        "metadata": tool_metadata  # Include SQL queries, RAG results etc.
                    }

                elif node_name == "respond":
                    # Final response
                    messages = node_output.get("messages", [])
                    if messages:
                        last_msg = messages[-1]
                        if hasattr(last_msg, 'content'):
                            # Stream content chunk by chunk
                            content = _message_text(last_msg)
                            if content:
                                chunk_size = 50
                                for i in range(0, len(content), chunk_size):
                                    chunk = content[i:i+chunk_size]
                                    yield {
                                        "type": "response",
                                        "content": chunk
                                    }


# create singleton instance
orchestrator = Orchestrator()


if __name__ == "__main__":
    # Quick test from the command line (the chat UI at http://localhost:8000 is the normal way to ask):
    #   python app/tools/orchestrator.py "How much did I spend on coffee in March 2026?" --user 6
    import argparse

    parser = argparse.ArgumentParser(description = "Ask the FinFlow orchestrator a question")
    parser.add_argument("query", nargs = "?", default = "How much did I spend on coffee in March 2026?")
    parser.add_argument("--user", type = int, default = 6, help = "User ID to ask as")
    args = parser.parse_args()

    async def main():
        async for event in orchestrator.process_query(args.query, args.user):
            print(event)

    asyncio.run(main())