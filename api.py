import uuid
from datetime import datetime

from flask import request, current_app
from flask_restx import Namespace, Resource

from CTFd.utils import user as current_user
from CTFd.utils.decorators import admins_only, authed_only
from .control_utils import ControlUtil
from .db_utils import DBUtils
from .models import DynamicDockerChallenge
from .redis_utils import RedisUtils

admin_namespace = Namespace("ctfd-whale-admin")
user_namespace = Namespace("ctfd-whale-user")


@admin_namespace.route('/settings')
class AdminSettings(Resource):
    @admins_only
    def patch(self):
        req = request.get_json()
        DBUtils.save_all_configs(req.items())
        redis_util = RedisUtils(app=current_app)
        redis_util.init_redis_port_sets()
        return {'success': True}


@admin_namespace.route('/container')
class AdminContainers(Resource):
    @staticmethod
    @admins_only
    def get():
        page = abs(request.args.get("page", 1, type=int))
        results_per_page = abs(request.args.get("per_page", 50, type=int))
        page_start = results_per_page * (page - 1)
        page_end = results_per_page * (page - 1) + results_per_page

        count = DBUtils.get_all_alive_container_count()
        containers = DBUtils.get_all_alive_container_page(
            page_start, page_end)

        return {'success': True, 'data': {
            'containers': containers,
            'total': count,
            'pages': int(count / results_per_page) + (count % results_per_page > 0),
            'page_start': page_start,
        }}

    @staticmethod
    @admins_only
    def patch():
        user_id = request.args.get('user_id')
        challenge_id = request.args.get('challenge_id')
        DBUtils.renew_current_container(
            user_id=user_id, challenge_id=challenge_id)
        return {'success': True}

    @staticmethod
    @admins_only
    def delete():
        user_id = request.args.get('user_id')
        ControlUtil.remove_container(current_app, user_id)
        return {'success': True}


@user_namespace.route("/container")
class UserContainers(Resource):
    @staticmethod
    @authed_only
    def get():
        user_id = current_user.get_current_user().id
        challenge_id = request.args.get('challenge_id')
        ControlUtil.check_challenge(challenge_id, user_id)
        data = ControlUtil.get_container(user_id=user_id)
        configs = DBUtils.get_all_configs()
        domain = configs.get('frp_http_domain_suffix', "")
        timeout = int(configs.get("docker_timeout", "3600"))
        if data is not None:
            if int(data.challenge_id) != int(challenge_id):
                return {}
            dynamic_docker_challenge = DynamicDockerChallenge.query \
                .filter(DynamicDockerChallenge.id == data.challenge_id) \
                .first_or_404()
            lan_domain = str(user_id) + "-" + data.uuid
            if dynamic_docker_challenge.redirect_type == "http":
                if int(configs.get('frp_http_port', "80")) == 80:
                    return {'success': True, 'type': 'http', 'domain': data.uuid + domain,
                            'remaining_time': timeout - (datetime.now() - data.start_time).seconds,
                            'lan_domain': lan_domain}
                else:
                    return {'success': True, 'type': 'http',
                            'domain': data.uuid + domain + ":" + configs.get('frp_http_port', "80"),
                            'remaining_time': timeout - (datetime.now() - data.start_time).seconds,
                            'lan_domain': lan_domain}
            else:
                return {'success': True, 'type': 'redirect', 'ip': configs.get('frp_direct_ip_address', ""),
                        'port': data.port,
                        'remaining_time': timeout - (datetime.now() - data.start_time).seconds,
                        'lan_domain': lan_domain}
        else:
            return {'success': True}

    @staticmethod
    @authed_only
    def post():
        user_id = current_user.get_current_user().id
        redis_util = RedisUtils(app=current_app, user_id=user_id)

        if not redis_util.acquire_lock():
            return {'success': False, 'msg': 'Request Too Fast!'}

        if ControlUtil.frequency_limit():
            return {'success': False, 'msg': 'Frequency limit, You should wait at least 1 min.'}

        ControlUtil.remove_container(current_app, user_id)
        challenge_id = request.args.get('challenge_id')
        ControlUtil.check_challenge(challenge_id, user_id)

        configs = DBUtils.get_all_configs()
        current_count = DBUtils.get_all_alive_container_count()
        if int(configs.get("docker_max_container_count")) <= int(current_count):
            return {'success': False, 'msg': 'Max container count exceed.'}

        dynamic_docker_challenge = DynamicDockerChallenge.query \
            .filter(DynamicDockerChallenge.id == challenge_id) \
            .first_or_404()
        flag = "flag{" + str(uuid.uuid4()) + "}"
        if dynamic_docker_challenge.redirect_type == "http":
            ControlUtil.add_container(
                app=current_app, user_id=user_id, challenge_id=challenge_id, flag=flag)
        else:
            port = redis_util.get_available_port()
            ControlUtil.add_container(
                app=current_app, user_id=user_id, challenge_id=challenge_id, flag=flag, port=port)

        redis_util.release_lock()
        return {'success': True}

    @staticmethod
    @authed_only
    def patch():
        user_id = current_user.get_current_user().id
        redis_util = RedisUtils(app=current_app, user_id=user_id)
        if not redis_util.acquire_lock():
            return {'success': False, 'msg': 'Request Too Fast!'}

        if ControlUtil.frequency_limit():
            return {'success': False, 'msg': 'Frequency limit, You should wait at least 1 min.'}

        configs = DBUtils.get_all_configs()
        challenge_id = request.args.get('challenge_id')
        ControlUtil.check_challenge(challenge_id, user_id)
        docker_max_renew_count = int(configs.get("docker_max_renew_count"))
        container = ControlUtil.get_container(user_id)
        if container is None:
            return {'success': False, 'msg': 'Instance not found.'}
        if container.renew_count >= docker_max_renew_count:
            return {'success': False, 'msg': 'Max renewal times exceed.'}
        ControlUtil.renew_container(user_id=user_id, challenge_id=challenge_id)
        redis_util.release_lock()
        return {'success': True}

    @staticmethod
    @authed_only
    def delete():
        user_id = current_user.get_current_user().id
        redis_util = RedisUtils(app=current_app, user_id=user_id)
        if not redis_util.acquire_lock():
            return {'success': False, 'msg': 'Request Too Fast!'}

        if ControlUtil.frequency_limit():
            return {'success': False, 'msg': 'Frequency limit, You should wait at least 1 min.'}

        if ControlUtil.remove_container(current_app, user_id):
            redis_util.release_lock()

            return {'success': True}
        else:
            return {'success': False, 'msg': 'Failed when destroy instance, please contact admin!'}
