from pydantic import BaseModel, ConfigDict, Field, StrictInt


class TelegramObject(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TelegramUser(TelegramObject):
    id: StrictInt = Field(gt=0)
    is_bot: bool = False


class Chat(TelegramObject):
    id: StrictInt
    type: str


class Photo(TelegramObject):
    file_id: str = Field(min_length=1, max_length=256)
    file_unique_id: str = Field(min_length=1, max_length=256)
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    file_size: StrictInt | None = Field(default=None, ge=0)


class Document(TelegramObject):
    file_id: str = Field(min_length=1, max_length=256)
    file_unique_id: str = Field(min_length=1, max_length=256)
    mime_type: str | None = None
    file_size: StrictInt | None = Field(default=None, ge=0)


class Message(TelegramObject):
    message_id: StrictInt = Field(gt=0)
    date: StrictInt = Field(ge=0)
    from_user: TelegramUser | None = Field(default=None, alias="from")
    chat: Chat
    text: str | None = Field(default=None, max_length=4096)
    caption: str | None = Field(default=None, max_length=1024)
    photo: list[Photo] = Field(default_factory=list, max_length=20)
    document: Document | None = None


class Callback(TelegramObject):
    id: str = Field(min_length=1, max_length=256)
    from_user: TelegramUser = Field(alias="from")
    message: Message | None = None
    data: str | None = Field(default=None, max_length=64)


class Update(TelegramObject):
    update_id: StrictInt = Field(ge=0)
    message: Message | None = None
    callback_query: Callback | None = None

    def identity(self) -> tuple[int, int, int]:
        if self.callback_query and self.callback_query.message:
            cb = self.callback_query
            return cb.from_user.id, cb.message.chat.id, cb.message.message_id
        if self.message and self.message.from_user:
            return self.message.from_user.id, self.message.chat.id, self.message.message_id
        raise ValueError("Missing identity")
