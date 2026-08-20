from neo4j import GraphDatabase

from app.config.settings import settings


class Neo4jClient:
    _driver = None
    database_name = settings.NEO4J_DATABASE  # Lưu lại để dùng lúc query

    @classmethod
    def connect(cls):
        """Hàm tự động khởi tạo và kiểm tra kết nối tới Neo4j"""
        if not all(
            [
                settings.NEO4J_URI,
                settings.NEO4J_USERNAME,
                settings.NEO4J_PASSWORD,
                settings.NEO4J_DATABASE,
            ]
        ):
            raise ValueError("Thiếu thông tin cấu hình Neo4j trong file .env!")

        if cls._driver is None:
            cls._driver = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD),
            )
            # Ping đến server để đảm bảo kết nối thành công ngay lúc start app
            cls._driver.verify_connectivity()

        return cls._driver

    @classmethod
    def get_driver(cls):
        if cls._driver is None:
            cls.connect()
        return cls._driver

    @classmethod
    def close_driver(cls):
        if cls._driver:
            cls._driver.close()
            cls._driver = None
