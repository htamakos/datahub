import collections
import dataclasses
from datetime import datetime
from typing import Callable, Counter, Generic, List, Optional, TypeVar

import pydantic

import datahub.emitter.mce_builder as builder
from datahub.configuration.common import AllowDenyPattern
from datahub.configuration.time_window_config import (
    BaseTimeWindowConfig,
    BucketDuration,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.metadata.schema_classes import (
    ChangeTypeClass,
    DatasetFieldUsageCountsClass,
    DatasetUsageStatisticsClass,
    DatasetUserUsageCountsClass,
    TimeWindowSizeClass,
)

ResourceType = TypeVar("ResourceType")


@dataclasses.dataclass
class GenericAggregatedDataset(Generic[ResourceType]):
    bucket_start_time: datetime
    resource: ResourceType
    user_email_pattern: AllowDenyPattern = AllowDenyPattern.allow_all()

    readCount: int = 0
    queryCount: int = 0

    queryFreq: Counter[str] = dataclasses.field(default_factory=collections.Counter)
    userFreq: Counter[str] = dataclasses.field(default_factory=collections.Counter)
    columnFreq: Counter[str] = dataclasses.field(default_factory=collections.Counter)

    total_budget_for_query_list: int = 24000
    query_trimmer_string_space: int = 10
    query_trimmer_string: str = " ..."

    def add_read_entry(
        self,
        user_email: str,
        query: Optional[str],
        fields: List[str],
    ) -> None:

        if not self.user_email_pattern.allowed(user_email):
            return

        self.readCount += 1
        self.userFreq[user_email] += 1

        if query:
            self.queryCount += 1
            self.queryFreq[query] += 1
        for column in fields:
            self.columnFreq[column] += 1

    def trim_query(self, query: str, budget_per_query: int) -> str:
        trimmed_query = query
        if len(query) > budget_per_query:
            if budget_per_query - self.query_trimmer_string_space > 0:
                end_index = budget_per_query - self.query_trimmer_string_space
                trimmed_query = query[:end_index] + self.query_trimmer_string
            else:
                raise Exception(
                    "Budget per query is too low. Please, decrease the number of top_n_queries."
                )
        return trimmed_query

    def make_usage_workunit(
        self,
        bucket_duration: BucketDuration,
        urn_builder: Callable[[ResourceType], str],
        top_n_queries: int,
    ) -> MetadataWorkUnit:

        budget_per_query: int = int(self.total_budget_for_query_list / top_n_queries)

        usageStats = DatasetUsageStatisticsClass(
            timestampMillis=int(self.bucket_start_time.timestamp() * 1000),
            eventGranularity=TimeWindowSizeClass(unit=bucket_duration, multiple=1),
            uniqueUserCount=len(self.userFreq),
            totalSqlQueries=self.queryCount,
            topSqlQueries=[
                self.trim_query(query, budget_per_query)
                for query, _ in self.queryFreq.most_common(top_n_queries)
            ],
            userCounts=[
                DatasetUserUsageCountsClass(
                    user=builder.make_user_urn(user_email.split("@")[0]),
                    count=count,
                    userEmail=user_email,
                )
                for user_email, count in self.userFreq.most_common()
            ],
            fieldCounts=[
                DatasetFieldUsageCountsClass(
                    fieldPath=column,
                    count=count,
                )
                for column, count in self.columnFreq.most_common()
            ],
        )

        mcp = MetadataChangeProposalWrapper(
            entityType="dataset",
            aspectName="datasetUsageStatistics",
            changeType=ChangeTypeClass.UPSERT,
            entityUrn=urn_builder(self.resource),
            aspect=usageStats,
        )

        return MetadataWorkUnit(
            id=f"{self.bucket_start_time.isoformat()}-{self.resource}", mcp=mcp
        )


class BaseUsageConfig(BaseTimeWindowConfig):
    top_n_queries: pydantic.PositiveInt = 10
    user_email_pattern: AllowDenyPattern = AllowDenyPattern.allow_all()
    include_operational_stats: bool = True

    @pydantic.validator("top_n_queries")
    def ensure_top_n_queries_is_not_too_big(cls, v: int) -> int:
        minimum_query_size = 20

        max_queries = int(
            GenericAggregatedDataset.total_budget_for_query_list / minimum_query_size
        )
        if (
            int(GenericAggregatedDataset.total_budget_for_query_list / v)
            < minimum_query_size
        ):
            raise ValueError(
                f"top_n_queries is set to {v} but it can be maximum {max_queries}"
            )
        return v
