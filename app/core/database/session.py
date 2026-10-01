from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: mapped domain objects stay readable after commit.
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
