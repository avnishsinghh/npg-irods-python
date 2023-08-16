# -*- coding: utf-8 -*-
#
# Copyright © 2023 Genome Research Ltd. All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# @author Keith James <kdj@sanger.ac.uk>

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, unique
from typing import Iterator, Optional, Type

from partisan.irods import AVU, Collection, DataObject
from sqlalchemy import asc
from sqlalchemy.orm import Session
from structlog import get_logger

from npg_irods.common import infer_zone, update_metadata, update_permissions
from npg_irods.db.mlwh import IseqFlowcell, IseqProductMetrics, Sample, Study
from npg_irods.metadata.common import SeqConcept, SeqSubset
from npg_irods.metadata.illumina import Instrument
from npg_irods.metadata.lims import (
    ensure_consent_withdrawn,
    has_consent_withdrawn_metadata,
    make_sample_acl,
    make_sample_metadata,
    make_study_metadata,
)

log = get_logger(__package__)


@unique
class TagIndex(Enum):
    """Sequencing tag indexes which have special meaning or behaviour."""

    BIN = 0
    """Tag index 0 is not a real tag i.e. there is no DNA sequence corresponding to it.
    Rather, it is a bin for reads that cannot be associated with any of the candidate
    tags in a pool after sequencing."""

    CONTROL_198 = 198

    CONTROL_888 = 888
    """Tag index 888 is conventionally used to indicate a control sample e.g. Phi X
    that has been added to a pool."""


@dataclass(order=True)
class Component:
    """A set of reads from an Illumina sequencing run."""

    id_run: int
    """The run ID generated by WSI tracking database."""

    position: int
    """The 1-based instrument position where the sample was sequenced."""

    tag_index: Optional[int]
    """The 1-based index in a pool of tags, if multiplexed."""

    subset: Optional[SeqSubset]
    """The subset of the reads for this run/position/tag index, if filtered."""

    @classmethod
    def from_avu(cls, avu: AVU):
        """Return a new Component instance by parsing the value of an Illumina
        `component` AVU from iRODS."""
        try:
            if avu.attribute != SeqConcept.COMPONENT.value:
                raise ValueError(
                    f"Cannot create a Component from metadata {avu}; "
                    f"invalid attribute {avu.attribute}"
                )

            avu_value = json.loads(avu.value)
            subset = avu_value.get(SeqConcept.SUBSET.value, None)

            return Component(
                avu_value[Instrument.RUN.value],
                avu_value[SeqConcept.POSITION.value],
                tag_index=avu_value.get(SeqConcept.TAG_INDEX.value, None),
                subset=subset,
            )
        except Exception as e:
            raise ValueError(
                f"Failed to create a Component from metadata {avu}: {e}",
            ) from e

    def __init__(
        self, id_run: int, position: int, tag_index: int = None, subset: str = None
    ):
        self.id_run = id_run
        self.position = position
        self.tag_index = int(tag_index) if tag_index is not None else None

        match subset:
            case SeqSubset.HUMAN.value:
                self.subset = SeqSubset.HUMAN
            case SeqSubset.XAHUMAN.value:
                self.subset = SeqSubset.XAHUMAN
            case SeqSubset.YHUMAN.value:
                self.subset = SeqSubset.YHUMAN
            case SeqSubset.PHIX.value:
                self.subset = SeqSubset.PHIX
            case None:
                self.subset = None
            case _:
                raise ValueError(f"Invalid subset '{subset}'")

    def contains_nonconsented_human(self):
        """Return True if this component contains non-consented human sequence."""
        return self.subset is not None and self.subset in [
            SeqSubset.HUMAN,
            SeqSubset.XAHUMAN,
        ]

    def __repr__(self):
        rep = {
            Instrument.RUN.value: self.id_run,
            SeqConcept.POSITION.value: self.position,
        }
        if self.tag_index is not None:
            rep[SeqConcept.TAG_INDEX.value] = self.tag_index
        if self.subset is not None:
            rep[SeqConcept.SUBSET.value] = self.subset

        return json.dumps(rep, sort_keys=True, separators=(",", ":"))


def ensure_secondary_metadata_updated(
    item: Collection | DataObject, mlwh_session, include_controls=False
) -> bool:
    """Update iRODS secondary metadata and permissions on Illumina run collections
    and data objects.

    Prerequisites:
      - The instance has `component` metadata (used to identify the constituent
    run / position / tag index components of the data).

    - Instances relating to a single sample instance e.g. a cram file for a single
    plex from a pool that has been de-multiplexed by identifying its indexing tag(s),
    will get sample metadata appropriate for that single sample. They will get study
    metadata (which includes appropriate opening of access controls) for the
    single study that sample is a member of.

    - Instances relating to multiple samples that were sequenced separately and
    then had their sequence data merged will get sample metadata appropriate to all
    the constituent samples. They will get study metadata (which includes
    appropriate opening of access controls) only if all the samples are from the
    same study. A data object with mixed-study data will not be made accessible.

    - Instances which contain control data from spiked-in controls e.g. Phi X
    where the control was not added as a member of a pool are treated as any other
    data object derived from the sample they were mixed with. They get no special
    treatment for metadata or permissions and are not considered members of any
    control study.

    - Instances which contain control data from spiked-in controls e.g. Phi X
    where the control was added as a member of a pool (typically with tag index 198
    or 888) are treated as any other member of a pool and have their own identity as
    samples in LIMS. They get no special treatment for metadata or permissions and
    are considered members the appropriate control study.

    - Instances which contain human data lacking explicit consent ("unconsented")
    are treated the same way as human samples with consent withdrawn with respect to
    permissions i.e. all access permissions are removed, leaving only permissions
    for the current user (who is making these changes) and for any rodsadmin users
    who currently have access.

    Args:
        item: A Collection or DataObject.
        mlwh_session: An open SQL session.
        include_controls: If True, include any control samples in the metadata and
        permissions.

    Returns:
       True if updated.
    """
    zone = infer_zone(item)
    secondary_metadata, acl = [], []

    components = [
        Component.from_avu(avu) for avu in item.metadata(SeqConcept.COMPONENT)
    ]  # Illumina specific
    for c in components:
        for fc in find_flowcells_by_component(
            mlwh_session, c, include_controls=include_controls
        ):
            secondary_metadata.extend(make_sample_metadata(fc.sample))
            secondary_metadata.extend(make_study_metadata(fc.study))
            acl.extend(make_sample_acl(fc.sample, fc.study, zone=zone))

    meta_update = update_metadata(item, secondary_metadata)

    cons_update = xahu_update = perm_update = False
    if has_consent_withdrawn_metadata(item):
        log.info("Consent withdrawn", path=item)
        cons_update = ensure_consent_withdrawn(item)
    elif any(c.contains_nonconsented_human() for c in components):  # Illumina specific
        log.info("Non-consented human data", path=item)
        xahu_update = ensure_consent_withdrawn(item)
    else:
        perm_update = update_permissions(item, acl)

    return any([meta_update, cons_update, xahu_update, perm_update])


def find_flowcells_by_component(
    sess: Session, component: Component, include_controls=False
) -> list[Type[IseqFlowcell]]:
    """Query the ML warehouse for flowcell information for the given component.

    Args:
        sess: An open SQL session.
        component: A component
        include_controls: If True, add parameters to the query to include spiked-in
        controls in the result.

    Returns:
        The associated flowcells.
    """
    query = (
        sess.query(IseqFlowcell)
        .distinct()
        .join(IseqFlowcell.iseq_product_metrics)
        .filter(IseqProductMetrics.id_run == component.id_run)
    )

    if component.position is not None:
        query = query.filter(IseqProductMetrics.position == component.position)

    match component.tag_index:
        case TagIndex.CONTROL_198.value | TagIndex.CONTROL_888.value if include_controls:
            query = query.filter(IseqProductMetrics.tag_index == component.tag_index)
        case TagIndex.CONTROL_198.value | TagIndex.CONTROL_888.value:
            raise ValueError(
                "Attempted to exclude controls for a query specifically requesting "
                f"control tag index {component.tag_index}"
            )
        case TagIndex.BIN.value:
            query = query.filter(IseqProductMetrics.tag_index.is_not(None))
        case int():
            query = query.filter(IseqProductMetrics.tag_index == component.tag_index)
        case None:
            query = query.filter(IseqProductMetrics.tag_index.is_(None))
        case _:
            raise ValueError(f"Invalid tag index {component.tag_index}")

    return query.order_by(asc(IseqFlowcell.id_iseq_flowcell_tmp)).all()


def find_components_changed(sess: Session, since: datetime) -> Iterator[Component]:
    """Find in the ML warehouse any Illumina sequence components whose tracking
    metadata has been changed since a given time.

    A change is defined as the "recorded_at" column (Sample, Study, IseqFlowcell) or
    "last_changed" colum (IseqProductMetrics) having a timestamp more recent than the
    given time.

    Args:
        sess: An open SQL session.
        since: A datetime query argument.

    Returns:
        An iterator over Components whose tracking metadata have changed.
    """
    for rpt in (
        sess.query(
            IseqProductMetrics.id_run, IseqFlowcell.position, IseqFlowcell.tag_index
        )
        .distinct()
        .join(IseqFlowcell.sample)
        .join(IseqFlowcell.study)
        .join(IseqFlowcell.iseq_product_metrics)
        .filter(
            (Sample.recorded_at >= since)
            | (Study.recorded_at >= since)
            | (IseqFlowcell.recorded_at >= since)
            | (IseqProductMetrics.last_changed >= since)
        )
        .order_by(asc(IseqFlowcell.id_iseq_flowcell_tmp))
    ):
        yield Component(*rpt)
