"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.
"""
from sqlalchemy.orm import sessionmaker, scoped_session


class ThreadedSession:

    def __init__(self, sqlalchemy_engine) -> None:
        self.engine = sqlalchemy_engine

    def __enter__(self):
        session_factory = sessionmaker(bind=self.engine)
        self.session = scoped_session(session_factory)
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.remove()