from pathlib import Path

from pydantic import BaseModel, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class MailSettings(BaseModel):
    account: str
    imap: str
    scope: str


class LlmSettings(BaseModel):
    provider: str
    voter_model: str
    voter_temperature: float = 0.4
    n_voters: int = 3
    agreement_threshold: float = 1.0
    arbiter_model: str


class AgentSettings(BaseModel):
    name: str
    host: str
    port: int
    version: str
    llm: LlmSettings


class SqlSettings(BaseModel):
    host: str
    port: int
    user: str
    password: SecretStr
    name: str


class GatewaySettings(BaseModel):
    """LiteLLM gateway exposing the Anthropic-compatible /v1/messages route."""

    url: str
    key: SecretStr


class S3Settings(BaseModel):
    """SeaweedFS / MinIO / AWS-S3 endpoint. Holds the report image archive."""

    endpoint: str
    browser: str
    region: str
    bucket: str
    access_key: SecretStr
    secret_key: SecretStr


class Settings(BaseSettings):
    model_config = SettingsConfigDict(yaml_file=CONFIG_PATH, env_nested_delimiter="__")

    mail: MailSettings
    agents: list[AgentSettings] = []
    sql: SqlSettings
    gateway: GatewaySettings
    s3: S3Settings

    def agent_config_by_name(self, name: str) -> AgentSettings:
        for agent in self.agents:
            if agent.name == name:
                return agent
        raise ValueError(f"No agent named {name!r} in {CONFIG_PATH}")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


settings = Settings()
