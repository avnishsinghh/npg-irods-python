#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright © 2022 Genome Research Ltd. All rights reserved.
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
# @author Michael Kubiak <mk35@sanger.ac.uk>

import json

import structlog
from typing import List, Dict
from multiprocessing import Pool, pool
from partisan.irods import DataObject, Collection, client_pool

log = structlog.get_logger(__file__)

JSON_FILE_VERSION = "1.0"
ILLUMINA = "illumina"
NPG_PROD = "npg-prod"


def create_product_dict(obj_path: str, ext: str) -> Dict:
    """
    Gathers information about a data object that is required to load
    it into the seq_product_irods_locations table.
    """
    # rebuild un-pickleable objects inside subprocess
    with client_pool(1) as baton_pool:
        obj = DataObject(obj_path, baton_pool)
        if obj.name.split(".")[-1] == ext and "ranger" not in str(obj.path):
            product = {
                "seq_platform_name": ILLUMINA,
                "pipeline_name": NPG_PROD,
                "irods_root_collection": str(obj.path),
                "irods_data_relative_path": str(obj.name),
            }

            for meta in obj.metadata():
                # Check for unwanted files
                if (
                    (meta.attribute == "tag_index" and meta.value == "0")
                    or (meta.attribute == "reference" and "PhiX" in meta.value)
                    or (
                        # subset is not present alone, but is part of the component metadata
                        meta.attribute == "component"
                        and "subset" in meta.value
                    )
                ):
                    raise ExcludedObjectException(
                        f"{obj} is in an excluded object class"
                    )

                if meta.attribute == "id_product":
                    product["id_product"] = meta.value
                if meta.attribute == "alt_process":
                    product["pipeline_name"] = f"alt_{meta.value}"

            if "id_product" in product.keys():
                return product
            else:
                # The error is only raised when the ApplyResult object
                # has its .get method run, so can be handled (logged)
                # in the main process
                raise MissingMetadataError(f"id_product metadata not found for {obj}")
        else:
            raise ExcludedObjectException(f"{obj} is in an excluded class")


def extract_products(results: List[pool.ApplyResult]) -> List[Dict]:
    """
    Extracts products from result list and handles errors raised.
    """
    products = []
    for result in results:
        try:
            product = result.get()
            if product is not None:
                products.append(product)
        except MissingMetadataError as error:
            log.warn(error)
        except ExcludedObjectException as error:
            log.debug(error)
    return products


def find_products(coll: Collection, processes: int) -> List[dict]:
    """
    Recursively finds all (non-human, non-phix) cram data objects in
    a collection.
    Runs a pool of processes to create a list of dictionaries containing
    information to load them into the seq_product_irods_locations table.
    """

    with Pool(processes) as p:
        cram_results = [
            p.apply_async(create_product_dict, (str(obj), "cram"))
            for obj in coll.iter_contents()
            if isinstance(obj, DataObject)
        ]
        products = extract_products(cram_results)

        if not products:
            log.warn(f"No cram files found in {coll}, searching for bam files")
            bam_results = [
                p.apply_async(create_product_dict, (str(obj), "bam"))
                for obj in coll.iter_contents()
                if not isinstance(obj, Collection)
            ]
            products = extract_products(bam_results)

    return products


def generate_files(colls: List[str], processes: int, out_file: str):

    log.info(
        f"Creating product rows for products in {colls} to output into {out_file} this is more test"
    )
    products = []
    with client_pool(1) as baton_pool:
        for coll_path in colls:
            coll = Collection(coll_path, baton_pool)
            if coll.exists():
                # find all contained products and get metadata
                coll_products = find_products(coll, processes)
                products.extend(coll_products)
                log.info(f"Found {len(coll_products)} products in {coll}")
            else:
                log.warn(f"collection {coll} not found")
    mlwh_json = {"version": JSON_FILE_VERSION, "products": products}
    with open(out_file, "w") as out:
        json.dump(mlwh_json, out)


class MissingMetadataError(Exception):
    """Raise when expected metadata is not present on an object."""

    pass


class ExcludedObjectException(Exception):
    """
    Raise when an object is one of the excluded set:

    - Has tag 0
    - Reference is PhiX (mostly controls)
    - Is a subset (such as 'phix' or 'human')

    """

    pass
