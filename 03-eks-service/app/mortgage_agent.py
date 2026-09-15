import argparse
import logging
import os
from datetime import date, timedelta
from functools import lru_cache

import boto3
from strands import Agent, tool
from strands_tools import calculator, retrieve


MODEL_ID = os.environ.get(
    "MODEL_ID",
    "us.anthropic.claude-sonnet-4-6",
)
KB_PARAMETER_NAME = os.environ.get(
    "KB_PARAMETER_NAME",
    "/app/mortgage_assistant/kb_id",
)


def configure_logging() -> None:
    for logger_name in (
        "strands",
        "strands.agent",
        "strands.tools",
        "strands.models",
        "strands.bedrock",
    ):
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                )
            )
            logger.addHandler(handler)


@lru_cache(maxsize=1)
def get_knowledge_base_id() -> str:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    try:
        parameter = boto3.client("ssm", region_name=region).get_parameter(
            Name=KB_PARAMETER_NAME
        )
    except Exception as error:
        raise RuntimeError(
            f"Unable to retrieve the Knowledge Base ID from "
            f"{KB_PARAMETER_NAME}: {error}"
        ) from error

    knowledge_base_id = parameter["Parameter"]["Value"].strip()
    if not knowledge_base_id:
        raise RuntimeError(f"SSM parameter {KB_PARAMETER_NAME} is empty")

    os.environ["KNOWLEDGE_BASE_ID"] = knowledge_base_id
    return knowledge_base_id


@tool
def answer_general_mortgage_questions(query: str) -> str:
    """Answer general mortgage questions using the workshop Knowledge Base."""
    get_knowledge_base_id()
    agent = Agent(
        model=MODEL_ID,
        tools=[retrieve],
        system_prompt="""
        You are a mortgage information assistant.

        Always use the retrieve tool before answering a mortgage question.
        Answer only from information returned by the workshop Knowledge Base.
        If the Knowledge Base does not contain the answer, say "I don't know."
        Explain concepts in plain language, present balanced tradeoffs, and make
        it clear that general information is not personalized financial advice.
        """,
    )
    return str(agent(query))


@tool
def get_mortgage_details(customer_id: str) -> dict:
    """Return mock existing-mortgage data for the workshop."""
    today = date.today()
    return {
        "account_number": customer_id,
        "outstanding_principal": 150000.0,
        "interest_rate": 4.5,
        "maturity_date": "2030-06-30",
        "payments_remaining": 72,
        "last_payment_date": str(today - timedelta(days=30)),
        "next_payment_due": str(today + timedelta(days=1)),
        "next_payment_amount": 1250.0,
    }


@tool
def answer_existing_mortgage_questions(query: str) -> str:
    """Answer questions about a mock customer's existing mortgage."""
    agent = Agent(
        model=MODEL_ID,
        tools=[get_mortgage_details],
        system_prompt="""
        You are an existing-mortgage assistant.

        Ask for a customer ID before using the mortgage-details tool. Explain
        balances, rates, payment dates, and payoff information clearly. The
        returned account data is mock workshop data. Do not invent account
        information that was not returned by a tool.
        """,
    )
    return str(agent(query))


@tool
def get_mortgage_app_doc_status(customer_id: str | None = None) -> list[dict]:
    """Return mock required-document status for a mortgage application."""
    return [
        {"type": "proof_of_income", "status": "COMPLETED"},
        {"type": "employment_information", "status": "MISSING"},
        {"type": "proof_of_assets", "status": "COMPLETED"},
        {"type": "credit_information", "status": "COMPLETED"},
    ]


@tool
def get_application_details(customer_id: str | None = None) -> dict:
    """Return mock details about a mortgage application."""
    return {
        "customer_id": customer_id or "123456",
        "application_id": "998776",
        "application_date": str(date.today() - timedelta(days=35)),
        "application_status": "IN_PROGRESS",
        "application_type": "NEW_MORTGAGE",
        "name": "Workshop Customer",
    }


@tool
def create_customer_id() -> str:
    """Create a mock customer ID."""
    return "123456"


@tool
def create_loan_application(
    customer_id: str,
    name: str,
    age: int,
    annual_income: int,
    annual_expense: int,
) -> str:
    """Create a mock loan application."""
    return (
        f"Loan application created for {name} (customer {customer_id}); "
        f"age={age}, annual_income={annual_income}, "
        f"annual_expense={annual_expense}."
    )


@tool
def answer_new_loan_application_questions(query: str) -> str:
    """Handle new mortgage application questions."""
    agent = Agent(
        model=MODEL_ID,
        tools=[
            get_mortgage_app_doc_status,
            get_application_details,
            create_customer_id,
            create_loan_application,
        ],
        system_prompt="""
        You are a new mortgage application assistant.

        Ask for a customer ID first and create one if necessary. Collect name,
        age, annual income, and annual expenses one question at a time before
        creating an application. Use tools for all application data and never
        invent information that was not returned by a tool.
        """,
    )
    return str(agent(query))


def create_supervisor_agent() -> Agent:
    return Agent(
        model=MODEL_ID,
        tools=[
            answer_general_mortgage_questions,
            answer_existing_mortgage_questions,
            answer_new_loan_application_questions,
            calculator,
        ],
        system_prompt="""
        You are the supervisor for a mortgage assistant.

        Route general mortgage information questions to the general mortgage
        tool, existing-account questions to the existing mortgage tool, and new
        application questions to the application tool. Use the calculator for
        calculations. Present the selected tool's result as one clear response.
        """,
    )


def run_prompt(prompt: str) -> str:
    if not prompt or not prompt.strip():
        raise ValueError("Prompt must not be empty")
    return str(create_supervisor_agent()(prompt.strip()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Mortgage Assistant Agent")
    parser.add_argument(
        "--prompt",
        "-p",
        required=True,
        help="Prompt to send to the mortgage assistant.",
    )
    args = parser.parse_args()
    configure_logging()
    print(run_prompt(args.prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
