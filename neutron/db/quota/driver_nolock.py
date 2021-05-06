# Copyright (c) 2021 Red Hat Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from neutron_lib.db import api as db_api
from neutron_lib import exceptions
from oslo_log import log

from neutron.db.quota import api as quota_api
from neutron.db.quota import driver as quota_driver


LOG = log.getLogger(__name__)


class DbQuotaNoLockDriver(quota_driver.DbQuotaDriver):
    """Driver to enforce quotas and retrieve quota information

    This driver does not use a (resource, project_id) lock but relays on the
    simplicity of the calculation method, that is executed in a single database
    transaction. The method (1) counts the number of created resources and (2)
    the number of resource reservations. If the requested number of resources
    do not exceeds the quota, a new reservation register is created.

    This calculation method does not guarantee the quota enforcement if, for
    example, the database isolation level is read committed; two transactions
    can count the same number of resources and reservations, committing both
    a new reservation exceeding the quota. But the goal of this reservation
    method is to be fast enough to avoid the concurrency when counting the
    resources while not blocking concurrent API operations.
    """
    @db_api.retry_if_session_inactive()
    def make_reservation(self, context, project_id, resources, deltas, plugin):
        resources_over_limit = []
        with db_api.CONTEXT_WRITER.using(context):
            # Filter out unlimited resources.
            limits = self.get_tenant_quotas(context, resources, project_id)
            unlimited_resources = set([resource for (resource, limit) in
                                       limits.items() if limit < 0])
            requested_resources = (set(deltas.keys()) - unlimited_resources)

            # Delete expired reservations before counting valid ones. This
            # operation is fast and by calling it before making any
            # reservation, we ensure the freshness of the reservations.
            quota_api.remove_expired_reservations(context,
                                                  tenant_id=project_id)

            # Count the number of (1) used and (2) reserved resources for this
            # project_id. If any resource limit is exceeded, raise exception.
            for resource_name in requested_resources:
                tracked_resource = resources.get(resource_name)
                if not tracked_resource:
                    continue

                used_and_reserved = tracked_resource.count(
                    context, None, project_id, count_db_registers=True)
                resource_num = deltas[resource_name]
                if limits[resource_name] < (used_and_reserved + resource_num):
                    resources_over_limit.append(resource_name)

            if resources_over_limit:
                raise exceptions.OverQuota(overs=sorted(resources_over_limit))

            return quota_api.create_reservation(context, project_id, deltas)

    def cancel_reservation(self, context, reservation_id):
        quota_api.remove_reservation(context, reservation_id, set_dirty=False)
