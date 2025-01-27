import re

from databricks.labs.lsql.backends import MockBackend

from databricks.labs.ucx.recon.base import TableIdentifier, TableMetadata, ColumnMetadata
from databricks.labs.ucx.recon.metadata_retriever import DatabricksTableMetadataRetriever


def test_hms_table_metadata_retrieval(metadata_row_factory):
    table_identifier = TableIdentifier("hive_metastore", "db1", "table1")
    sql_backend = MockBackend(
        rows={
            "DESCRIBE TABLE": metadata_row_factory[
                ("col2", "string"),
                ("col1", "int"),
                ("col3", "array<string>"),
                ("col4", "struct<a:int,b:int,c:array<string>>"),
                ("# col_name", "data_type"),
            ]
        }
    )

    expected_metadata = TableMetadata(
        identifier=table_identifier,
        columns=[
            ColumnMetadata(name="col1", data_type="int"),
            ColumnMetadata(name="col2", data_type="string"),
            ColumnMetadata(name="col3", data_type="array<string>"),
            ColumnMetadata(name="col4", data_type="struct<a:int,b:int,c:array<string>>"),
        ],
    )

    metadata_retriever = DatabricksTableMetadataRetriever(sql_backend)
    actual_metadata = metadata_retriever.get_metadata(table_identifier)
    assert actual_metadata == expected_metadata


def test_unity_table_metadata_retrieval(metadata_row_factory):
    table_identifier = TableIdentifier("catalog1", "db1", "table1")
    sql_backend = MockBackend(
        rows={
            f"{table_identifier.catalog_escaped}.information_schema.columns": metadata_row_factory[
                ("col2", "string"),
                ("col1", "int"),
                ("col3", "array<string>"),
            ]
        }
    )

    expected_metadata = TableMetadata(
        identifier=table_identifier,
        columns=[
            ColumnMetadata(name="col1", data_type="int"),
            ColumnMetadata(name="col2", data_type="string"),
            ColumnMetadata(name="col3", data_type="array<string>"),
        ],
    )

    metadata_retriever = DatabricksTableMetadataRetriever(sql_backend)
    actual_metadata = metadata_retriever.get_metadata(table_identifier)
    assert actual_metadata == expected_metadata


def test_hms_metadata_query():
    table_identifier = TableIdentifier("hive_metastore", "db1", "table1")
    actual_query = DatabricksTableMetadataRetriever.build_metadata_query(table_identifier).strip().lower()
    expected_query = "DESCRIBE TABLE `hive_metastore`.`db1`.`table1`".lower()
    assert re.sub(r'\s+', ' ', actual_query) == expected_query


def test_unity_metadata_query():
    table_identifier = TableIdentifier("catalog1", "db1", "table1")
    actual_query = DatabricksTableMetadataRetriever.build_metadata_query(table_identifier).strip().lower()
    expected_query = """
        SELECT 
            LOWER(column_name) AS col_name, 
            full_data_type AS data_type
        FROM 
            `catalog1`.information_schema.columns
        WHERE
            LOWER(table_catalog)='catalog1' AND
            LOWER(table_schema)='db1' AND
            LOWER(table_name) ='table1'
        ORDER BY col_name
    """.strip().lower()

    assert re.sub(r'\s+', ' ', actual_query) == re.sub(r'\s+', ' ', expected_query)
