from beat import psgrn, pscmp
import numpy as num

from pyrocko.guts import Object, String, Float, Int, Tuple
from pyrocko.guts_array import Array

from pyrocko import crust2x2, gf, cake, orthodrome, trace
from pyrocko.cake import GradientLayer
from pyrocko.fomosto import qseis
from pyrocko.fomosto import qssp
#from pyrocko.fomosto import qseis2d

import logging
import shutil
import copy

logger = logging.getLogger('heart')

c = 299792458.  # [m/s]
km = 1000.
d2r = num.pi / 180.
err_depth = 0.1
err_velocities = 0.05

lambda_sensors = {
                'Envisat': 0.056,       # needs updating- no ressource file
                'ERS1': 0.05656461471698113,
                'ERS2': 0.056,          # needs updating
                'JERS': 0.23513133960784313,
                'RadarSat2': 0.055465772433
                }


class PickleableTrace(trace.Trace):

    def __reduce__(self):
        pickled_state = super(PickleableTrace, self).__reduce__()
        if self.ydata is not None:
            updated_state = pickled_state[2] + (self.ydata,)
        else:
            updated_state = pickled_state[2]

        return (pickled_state[0], pickled_state[1], updated_state)

    def __setstate__(self, state):
        super(PickleableTrace, self).__setstate__(state[0:-1])
        self.ydata = state[-1]


class RectangularSource(gf.DCSource, gf.seismosizer.Cloneable):
    '''
    Source for rectangular fault that unifies the necessary different source
    objects for teleseismic and geodetic computations.
    Reference point of the depth attribute is the top-center of the fault.
    '''
    width = Float.T(help='width of the fault [m]',
                    default=1. * km)
    length = Float.T(help='length of the fault [m]',
                    default=1. * km)
    slip = Float.T(help='slip of the fault [m]',
                    default=1.)
    opening = Float.T(help='opening of the fault [m]',
                    default=0.)

    @staticmethod
    def dipvector(dip, strike):
        return num.array(
                [num.cos(dip * d2r) * num.cos(strike * d2r),
                 -num.cos(dip * d2r) * num.sin(strike * d2r),
                  num.sin(dip * d2r)])

    @staticmethod
    def strikevector(strike):
        return num.array([num.sin(strike * d2r),
                          num.cos(strike * d2r),
                          0.])

    @staticmethod
    def center(top_depth, width, dipvector):
        '''
        Get fault center coordinates. If input depth referrs to top_depth.
        '''
        return num.array([0., 0., top_depth]) + 0.5 * width * dipvector

    @staticmethod
    def top_depth(depth, width, dipvector):
        '''
        Get top depth of the fault. If input depth referrs to center
        coordinates. (Patches Funktion needs input depth to be top_depth.)
        '''
        return num.array([0., 0., depth]) - 0.5 * width * dipvector

    def patches(self, n, m, datatype):
        '''
        Cut source into n by m sub-faults and return n times m SourceObjects.
        Discretization starts at shallow depth going row-wise deeper.
        datatype - 'geodetic' or 'seismic' determines the :py:class to be
        returned. Depth is being updated from top_depth to center depth.
        '''

        length = self.length / float(n)
        width = self.width / float(m)
        patches = []

        dip_vec = self.dipvector(self.dip, self.strike)
        strike_vec = self.strikevector(self.strike)

        for j in range(m):
            for i in range(n):
                sub_center = self.center(self.depth, width, dip_vec) + \
                            strike_vec * ((i + 0.5 - 0.5 * n) * length) + \
                            dip_vec * ((j + 0.5 - 0.5 * m) * width)

                if datatype == 'seismic':
                    patch = gf.RectangularSource(
                        lat=float(self.lat),
                        lon=float(self.lon),
                        east_shift=float(sub_center[0] + self.east_shift),
                        north_shift=float(sub_center[1] + self.north_shift),
                        depth=float(sub_center[2]),
                        strike=self.strike, dip=self.dip, rake=self.rake,
                        length=length, width=width, stf=self.stf,
                        time=self.time, slip=self.slip)
                elif datatype == 'geodetic':
                    patch = pscmp.PsCmpRectangularSource(
                        lat=self.lat,
                        lon=self.lon,
                        east_shift=float(sub_center[0] + self.east_shift),
                        north_shift=float(sub_center[1] + self.north_shift),
                        depth=float(sub_center[2]),
                        strike=self.strike, dip=self.dip, rake=self.rake,
                        length=length, width=width, slip=self.slip,
                        opening=self.opening)
                else:
                    raise Exception(
                        "Datatype not supported either: 'seismic/geodetic'")

                patches.append(patch)

        return patches


def adjust_fault_reference(source, input_depth='top'):
    '''
    Adjusts source depth and east/north-shifts variables of fault according to
    input_depth mode 'top/center'.
    Takes RectangularSources(beat, pyrocko, pscmp)
    Updates input source!
    '''
    RF = RectangularSource

    dip_vec = RF.dipvector(dip=source.dip, strike=source.strike)

    if input_depth == 'top':
        center = RF.center(top_depth=source.depth,
                       width=source.width,
                       dipvector=dip_vec)
    elif input_depth == 'center':
        center = num.array([0., 0., source.depth])

    source.update(east_shift=float(center[0] + source.east_shift),
                  north_shift=float(center[1] + source.north_shift),
                  depth=float(center[2]))


class Covariance(Object):
    '''
    Covariance of an observation.
    '''
    data = Array.T(shape=(None, None),
                    dtype=num.float,
                    help='Data covariance matrix',
                    optional=True)
    pred_g = Array.T(shape=(None, None),
                    dtype=num.float,
                    help='Model prediction covariance matrix, fault geometry',
                    optional=True)
    pred_v = Array.T(shape=(None, None),
                    dtype=num.float,
                    help='Model prediction covariance matrix, velocity model',
                    optional=True)
    total = Array.T(shape=(None, None),
                    dtype=num.float,
                    help='Total covariance of all components.',
                    optional=True)

    @property
    def total(self):
        if self.pred_g is None:
            self.pred_g = num.zeros_like(self.data)

        if self.pred_v is None:
            self.pred_v = num.zeros_like(self.data)

        return self.data + self.pred_g + self.pred_v

    def get_inverse(self):
        '''
        Add and invert different covariance Matrices.
        '''
        return num.linalg.inv(self.total)

    @property
    def log_determinant(self):
        '''
        Calculate the determinante of the covariance matrix according to
        the properties of a triangular matrix.
        '''
        cholesky = num.linalg.cholesky(self.total)
        return num.log(num.diag(cholesky)).sum()


class TeleseismicTarget(gf.Target):

    covariance = Covariance.T(default=Covariance.D(),
                              optional=True,
                              help=':py:class:`Covariance` that holds data'
                                   'and model prediction covariance matrixes')


class ArrivalTaper(trace.Taper):
    ''' Cosine arrival Taper.

    :param a: start of fading in; [s] before phase arrival
    :param b: end of fading in; [s] before phase arrival
    :param c: start of fading out; [s] after phase arrival
    :param d: end of fading out; [s] after phase arrival
    '''

    a = Float.T(default=15.)
    b = Float.T(default=10.)
    c = Float.T(default=50.)
    d = Float.T(default=55.)


class Filter(Object):
    lower_corner = Float.T(default=0.001)
    upper_corner = Float.T(default=0.1)
    order = Int.T(default=4)


class Parameter(Object):
    name = String.T(default='lon')
    lower = Array.T(shape=(None,),
                    dtype=num.float,
                    serialize_as='list',
                    default=num.array([0., 0.]))
    upper = Array.T(shape=(None,),
                    dtype=num.float,
                    serialize_as='list',
                    default=num.array([1., 1.]))
    testvalue = Array.T(shape=(None,),
                        dtype=num.float,
                        serialize_as='list',
                        default=num.array([0.5, 0.5]))

    def __call__(self):
        if self.lower is not None:
            for i in range(self.dimension):
                if self.testvalue[i] > self.upper[i] or \
                    self.testvalue[i] < self.lower[i]:
                    raise Exception('the testvalue of parameter "%s" has to be'
                        'within the upper and lower bounds' % self.name)

    @property
    def dimension(self):
        return self.lower.size

    def bound_to_array(self):
        return num.array([self.lower, self.testval, self.upper],
                         dtype=num.float)


class IFG(Object):
    '''
    Interferogram class as a dataset in the inversion.
    '''
    track = String.T(default='A')
    master = String.T(optional=True,
                      help='Acquisition time of master image YYYY-MM-DD')
    slave = String.T(optional=True,
                      help='Acquisition time of slave image YYYY-MM-DD')
    amplitude = Array.T(shape=(None,), dtype=num.float, optional=True)
    wrapped_phase = Array.T(shape=(None,), dtype=num.float, optional=True)
    incidence = Array.T(shape=(None,), dtype=num.float, optional=True)
    heading = Array.T(shape=(None,), dtype=num.float, optional=True)
    los_vector = Array.T(shape=(None, 3), dtype=num.float, optional=True)
    utmn = Array.T(shape=(None,), dtype=num.float, optional=True)
    utme = Array.T(shape=(None,), dtype=num.float, optional=True)
    lats = Array.T(shape=(None,), dtype=num.float, optional=True)
    lons = Array.T(shape=(None,), dtype=num.float, optional=True)
    satellite = String.T(default='Envisat')

    def __str__(self):
        s = 'IFG Acquisition Track: %s\n' % self.track
        s += '  timerange: %s - %s\n' % (self.master, self.slave)
        if self.lats is not None:
            s += '  number of pixels: %i\n' % self.lats.size
        return s

    @property
    def wavelength(self):
        return lambda_sensors[self.satellite]

    def update_los_vector(self):
        '''
        Calculate LOS vector for given incidence and heading.
        '''
        if self.incidence.all() and self.heading.all() is None:
            Exception('Incidence and Heading need to be provided!')

        Su = num.cos(num.deg2rad(self.incidence))
        Sn = - num.sin(num.deg2rad(self.incidence)) * \
             num.cos(num.deg2rad(self.heading - 270))
        Se = - num.sin(num.deg2rad(self.incidence)) * \
             num.sin(num.deg2rad(self.heading - 270))
        self.los_vector = num.array([Se, Sn, Su], dtype=num.float).T
        return self.los_vector


class DiffIFG(IFG):
    '''
    Differential Interferogram class as geodetic target for the calculation
    of synthetics.
    '''
    unwrapped_phase = Array.T(shape=(None,), dtype=num.float, optional=True)
    coherence = Array.T(shape=(None,), dtype=num.float, optional=True)
    reference_point = Tuple.T(2, Float.T(), optional=True)
    reference_value = Float.T(optional=True, default=0.0)
    displacement = Array.T(shape=(None,), dtype=num.float, optional=True)
    covariance = Covariance.T(optional=True,
                              help=':py:class:`Covariance` that holds data'
                                   'and model prediction covariance matrixes')
    odw = Array.T(
            shape=(None,),
            dtype=num.float,
            help='Overlapping data weights, additional weight factor to the'
                 'dataset for overlaps with other datasets',
            optional=True)


def init_targets(stations, channels=['T', 'Z'], sample_rate=1.0,
                 crust_inds=[0], interpolation='multilinear'):
    '''
    Initiate a list of target objects given a list of indexes to the
    respective GF store velocity model variation index (crust_inds).
    '''
    targets = [TeleseismicTarget(
        quantity='displacement',
        codes=(stations[sta_num].network,
                 stations[sta_num].station,
                 '%i' % crust_ind, channel),  # n, s, l, c
        lat=stations[sta_num].lat,
        lon=stations[sta_num].lon,
        azimuth=stations[sta_num].get_channel(channel).azimuth,
        dip=stations[sta_num].get_channel(channel).dip,
        interpolation=interpolation,
        store_id='%s_ak135_%.3fHz_%s' % (stations[sta_num].station,
                                                sample_rate, crust_ind))

        for channel in channels
            for crust_ind in crust_inds
                for sta_num in range(len(stations))]

    return targets


def vary_model(earthmod, err_depth=0.1, err_velocities=0.1,
               depth_limit=600 * km):
    '''
    Vary depth and velocities in the given source model by Gaussians with given
    2-sigma errors [percent]. Ensures increasing velocity with depth. Stops at
    the given depth_limit [m].
    Mantle discontinuity uncertainties are hardcoded.
    Returns: Varied Earthmodel
             Cost - Counts repetitions of cycles to ensure increasing layer
                    velocity, if high - unlikely velocities are too high
                    cost up to 10 are ok for crustal profiles.
    '''

    new_earthmod = copy.deepcopy(earthmod)
    layers = new_earthmod.layers()

    last_l = None
    cost = 0
    deltaz = 0

    # uncertainties in discontinuity depth after Shearer 1991
    discont_unc = {'410': 3 * km,
                   '520': 4 * km,
                   '660': 8 * km}

    # uncertainties in velocity for upper and lower mantle from Woodward 1991
    mantle_vel_unc = {'200': 0.02,     # above 200
                      '400': 0.01}     # above 400

    for layer in layers:
        # stop if depth_limit is reached
        if depth_limit:
            if layer.ztop >= depth_limit:
                layer.ztop = last_l.zbot
                # assign large cost if previous layer has higher velocity
                if layer.mtop.vp < last_l.mtop.vp or \
                   layer.mtop.vp > layer.mbot.vp:
                    cost = 1000
                # assign large cost if layer bottom depth smaller than top
                if layer.zbot < layer.ztop:
                    cost = 1000
                break
        repeat = 1
        count = 0
        while repeat:
            if count > 1000:
                break

            # vary layer velocity
            # check for layer depth and use hardcoded uncertainties
            for l_depth, vel_unc in mantle_vel_unc.items():
                if float(l_depth) * km < layer.ztop:
                    err_velocities = vel_unc
                    print err_velocities

            deltavp = float(num.random.normal(
                        0, layer.mtop.vp * err_velocities / 3., 1))

            if layer.ztop == 0:
                layer.mtop.vp += deltavp

            # ensure increasing velocity with depth
            if last_l:
                # gradient layer without interface
                if layer.mtop.vp == last_l.mbot.vp:
                    if layer.mbot.vp + deltavp < layer.mtop.vp:
                        count += 1
                    else:
                        layer.mbot.vp += deltavp
                        layer.mbot.vs += (deltavp /
                                                layer.mbot.vp_vs_ratio())
                        repeat = 0
                        cost += count
                elif layer.mtop.vp + deltavp < last_l.mbot.vp:
                    count += 1
                else:
                    layer.mtop.vp += deltavp
                    layer.mtop.vs += (deltavp / layer.mtop.vp_vs_ratio())
                    if isinstance(layer, GradientLayer):
                        layer.mbot.vp += deltavp
                        layer.mbot.vs += (deltavp / layer.mbot.vp_vs_ratio())
                    repeat = 0
                    cost += count
            else:
                repeat = 0

        # vary layer depth
        layer.ztop += deltaz
        repeat = 1

        # use hard coded uncertainties for mantle discontinuities
        if '%i' % (layer.zbot / km) in discont_unc:
            factor_d = discont_unc['%i' % (layer.zbot / km)] / layer.zbot
        else:
            factor_d = err_depth

        while repeat:
            # ensure that bottom of layer is not shallower than the top
            deltaz = float(num.random.normal(
                       0, layer.zbot * factor_d / 3., 1))  # 3 sigma
            layer.zbot += deltaz
            if layer.zbot < layer.ztop:
                layer.zbot -= deltaz
                count += 1
            else:
                repeat = 0
                cost += count

        last_l = copy.deepcopy(layer)

    return new_earthmod, cost


def ensemble_earthmodel(ref_earthmod, num_vary=10, err_depth=0.1,
                        err_velocities=0.1, depth_limit=600 * km):
    '''
    Create ensemble of earthmodels (num_vary) that vary around a given input
    pyrocko cake earth model by a Gaussian of std_depth (in Percent 0.1 = 10%)
    for the depth layers and std_velocities (in Percent) for the p and s wave
    velocities.
    '''

    earthmods = []
    i = 0
    while i < num_vary:
        new_model, cost = vary_model(ref_earthmod,
                                     err_depth,
                                     err_velocities,
                                     depth_limit)
        if cost > 20:
            print 'Skipped unlikely model', cost
        else:
            i += 1
            earthmods.append(new_model)

    return earthmods


def seis_construct_gf(station, event, superdir, code='qssp',
                source_depth_min=0., source_depth_max=10., source_spacing=1.,
                sample_rate=2., source_range=100., depth_limit=600 * km,
                earth_model='ak135-f-average.m', crust_ind=0,
                execute=False, rm_gfs=True, nworkers=1):
    '''Create a GF store for a station with respect to an event for a given
       Phase [P or S] and a distance range around the event.'''

    # calculate distance to station
    distance = orthodrome.distance_accurate50m(event, station)
    print 'Station', station.station
    print '---------------------'

    # load velocity profile from CRUST2x2 and check for water layer
    profile_station = crust2x2.get_profile(station.lat, station.lon)
    thickness_lwater = profile_station.get_layer(crust2x2.LWATER)[0]
    if thickness_lwater > 0.0:
        print 'Water layer', str(thickness_lwater), 'in CRUST model! \
                remove and add to lower crust'
        thickness_llowercrust = profile_station.get_layer(
                                        crust2x2.LLOWERCRUST)[0]
        thickness_lsoftsed = profile_station.get_layer(crust2x2.LSOFTSED)[0]

        profile_station.set_layer_thickness(crust2x2.LWATER, 0.0)
        profile_station.set_layer_thickness(crust2x2.LSOFTSED,
                num.ceil(thickness_lsoftsed / 3))
        profile_station.set_layer_thickness(crust2x2.LLOWERCRUST,
                thickness_llowercrust + \
                thickness_lwater + \
                (thickness_lsoftsed - num.ceil(thickness_lsoftsed / 3))
                )
        profile_station._elevation = 0.0
        print 'New Lower crust layer thickness', \
                str(profile_station.get_layer(crust2x2.LLOWERCRUST)[0])

    profile_event = crust2x2.get_profile(event.lat, event.lon)

    #extract model for source region
    source_model = cake.load_model(
        earth_model, crust2_profile=profile_event)

    # extract model for receiver stations,
    # lowest layer has to be as well in source layer structure!
    receiver_model = cake.load_model(
        earth_model, crust2_profile=profile_station)

    # randomly vary receiver site crustal model
    if crust_ind > 0:
        #moho_depth = receiver_model.discontinuity('moho').z
        receiver_model = ensemble_earthmodel(
                                        receiver_model,
                                        num_vary=1,
                                        err_depth=err_depth,
                                        err_velocities=err_velocities,
                                        depth_limit=depth_limit)[0]

    # define phases
    tabulated_phases = [gf.TPDef(
                            id='any_P',
                            definition='p,P,p\\,P\\'),
                        gf.TPDef(
                            id='any_S',
                            definition='s,S,s\\,S\\')]

    # fill config files for fomosto
    fom_conf = gf.ConfigTypeA(
        id='%s_%s_%.3fHz_%s' % (station.station,
                        earth_model.split('-')[0].split('.')[0],
                        sample_rate,
                        crust_ind),
        ncomponents=10,
        sample_rate=sample_rate,
        receiver_depth=0. * km,
        source_depth_min=source_depth_min * km,
        source_depth_max=source_depth_max * km,
        source_depth_delta=source_spacing * km,
        distance_min=distance - source_range * km,
        distance_max=distance + source_range * km,
        distance_delta=source_spacing * km,
        tabulated_phases=tabulated_phases)

   # slowness taper
    phases = [
        fom_conf.tabulated_phases[i].phases
        for i in range(len(
            fom_conf.tabulated_phases))]

    all_phases = []
    map(all_phases.extend, phases)

    mean_source_depth = num.mean((source_depth_min, source_depth_max))
    distances = num.linspace(fom_conf.distance_min,
                             fom_conf.distance_max,
                             100) * cake.m2d

    arrivals = receiver_model.arrivals(
                            phases=all_phases,
                            distances=distances,
                            zstart=mean_source_depth)

    ps = num.array(
        [arrivals[i].p for i in range(len(arrivals))])

    slownesses = ps / (cake.r2d * cake.d2m / km)

    slowness_taper = (0.0,
                      0.0,
                      1.1 * float(slownesses.max()),
                      1.3 * float(slownesses.max()))

    if code == 'qseis':
        from pyrocko.fomosto.qseis import build
        receiver_model = receiver_model.extract(depth_max=200 * km)
        model_code_id = code
        version = '2006a'
        conf = qseis.QSeisConfig(
            filter_shallow_paths=0,
            slowness_window=slowness_taper,
            wavelet_duration_samples=0.001,
            sw_flat_earth_transform=1,
            sw_algorithm=1,
            qseis_version=version)

    elif code == 'qssp':
        from pyrocko.fomosto.qssp import build
        source_model = copy.deepcopy(receiver_model)
        receiver_model = None
        model_code_id = code
        version = '2010beta'
        conf = qssp.QSSPConfig(
            qssp_version=version,
            slowness_max=float(num.max(slowness_taper)),
            toroidal_modes=True,
            spheroidal_modes=True,
            source_patch_radius=(fom_conf.distance_delta - \
                                 fom_conf.distance_delta * 0.05) / km)

    ## elif code == 'QSEIS2d':
    ##     from pyrocko.fomosto.qseis2d import build
    ##     model_code_id = 'qseis2d'
    ##     version = '2014'
    ##     conf = qseis2d.QSeis2dConfig()
    ##     conf.qseis_s_config.slowness_window = slowness_taper
    ##     conf.qseis_s_config.calc_slowness_window = 0
    ##     conf.qseis_s_config.receiver_max_distance = 11000.
    ##     conf.qseis_s_config.receiver_basement_depth = 35.
    ##     conf.qseis_s_config.sw_flat_earth_transform = 1
    ##     # extract method still buggy!!!
    ##     receiver_model = receiver_model.extract(
    ##             depth_max=conf.qseis_s_config.receiver_basement_depth * km)

    # fill remaining fomosto params
    fom_conf.earthmodel_1d = source_model.extract(depth_max='cmb')
    fom_conf.earthmodel_receiver_1d = receiver_model
    fom_conf.modelling_code_id = model_code_id + '.' + version

    window_extension = 60.   # [s]

    fom_conf.time_region = (
        gf.Timing(tabulated_phases[0].id + '-%s' % (1.1 * window_extension)),
        gf.Timing(tabulated_phases[1].id + '+%s' % (1.6 * window_extension)))
    fom_conf.cut = (
        gf.Timing(tabulated_phases[0].id + '-%s' % window_extension),
        gf.Timing(tabulated_phases[1].id + '+%s' % (1.5 * window_extension)))
    fom_conf.relevel_with_fade_in = True
    fom_conf.fade = (
        gf.Timing(tabulated_phases[0].id + '-%s' % (1.1 * window_extension)),
        gf.Timing(tabulated_phases[0].id + '-%s' % window_extension),
        gf.Timing(tabulated_phases[1].id + '+%s' % (1.5 * window_extension)),
        gf.Timing(tabulated_phases[1].id + '+%s' % (1.6 * window_extension)))

    fom_conf.validate()
    conf.validate()

    store_dir = superdir + fom_conf.id
    print 'create Store at ', store_dir
    gf.Store.create_editables(store_dir,
                              config=fom_conf,
                              extra={model_code_id: conf})
    if execute:
        store = gf.Store(store_dir, 'r')
        store.make_ttt()
        store.close()
        build(store_dir, nworkers=nworkers)
        gf_dir = store_dir + '/qssp_green'
        if rm_gfs:
            logger.info('Removing QSSP Greens Functions!')
            shutil.rmtree(gf_dir)


def geo_construct_gf(event, superdir,
                     source_distance_min=0., source_distance_max=100.,
                     source_depth_min=0., source_depth_max=40.,
                     source_distance_spacing=5., source_depth_spacing=0.5,
                     earth_model='ak135-f-average.m', crust_ind=0,
                     execute=True):
    '''
    Given a :py:class:`Event` the crustal model :py:class:`LayeredModel` from
    :py:class:`Crust2Profile` at the event location is extracted and the
    geodetic greens functions are calculated with the given grid resolution.
    '''
    c = psgrn.PsGrnConfigFull()

    n_steps_depth = (source_depth_max - source_depth_min) / \
                    source_depth_spacing
    n_steps_distance = (source_distance_max - source_distance_min) / \
                    source_distance_spacing

    c.distance_grid = psgrn.PsGrnSpatialSampling(
        n_steps=n_steps_distance,
        start_distance=source_distance_min,
        end_distance=source_distance_max)

    c.depth_grid = psgrn.PsGrnSpatialSampling(
        n_steps=n_steps_depth,
        start_distance=source_depth_min,
        end_distance=source_depth_max)

    c.sampling_interval = 10.

    # extract source crustal profile and check for water layer
    source_profile = crust2x2.get_profile(event.lat, event.lon)
    thickness_lwater = source_profile.get_layer(crust2x2.LWATER)[0]

    if thickness_lwater > 0.0:
        logger.info('Water layer', str(thickness_lwater), 'in CRUST model!'
            'remove and add to lower crust')
        thickness_llowercrust = source_profile.get_layer(
                                        crust2x2.LLOWERCRUST)[0]

        source_profile.set_layer_thickness(crust2x2.LWATER, 0.0)
        source_profile.set_layer_thickness(
            crust2x2.LLOWERCRUST,
            thickness_llowercrust + thickness_lwater)
        source_profile._elevation = 0.0

        logger.info('New Lower crust layer thickness', \
                str(source_profile.get_layer(crust2x2.LLOWERCRUST)[0]))

    source_model = cake.load_model(
        earth_model,
        crust2_profile=source_profile).extract(
           depth_max=source_depth_max * km)

    # potentially vary source model
    if crust_ind > 0:
        source_model = ensemble_earthmodel(
            source_model,
            num_vary=1,
            err_depth=err_depth,
            err_velocities=err_velocities,
            depth_limit=None)[0]

    c.earthmodel_1d = source_model
    c.psgrn_outdir = superdir + 'psgrn_green_%i/' % (crust_ind)
    c.validate()

    logger.info('Creating Geo GFs in directory: %s' % c.psgrn_outdir)

    runner = psgrn.PsGrnRunner(outdir=c.psgrn_outdir)
    if execute:
        runner.run(c)


def geo_layer_synthetics(store_superdir, crust_ind, lons, lats, sources,
                         keep_tmp=False):
    '''
    Input: Greensfunction store path, index of potentialy varied model store
           List of observation points Latitude and Longitude,
           List of rectangular fault sources.
    Output: NumpyArray(nobservations; ux, uy, uz)
    '''
    c = pscmp.PsCmpConfigFull()
    c.observation = pscmp.PsCmpScatter(lats=lats, lons=lons)
    c.psgrn_outdir = store_superdir + 'psgrn_green_%i/' % (crust_ind)

    # only coseismic displacement
    c.times_snapshots = [0]
    c.rectangular_source_patches = sources

    runner = pscmp.PsCmpRunner(keep_tmp=keep_tmp)
    runner.run(c)
    # returns list of displacements for each snapshot
    return runner.get_results(component='displ', flip_z=True)[0]


def get_phase_arrival_time(engine, source, target):
    '''
    Get arrival time from store for respective :py:class:`target`
    and :py:class:`source/event` pair. The channel of target determines
    if S or P wave.
    '''
    store = engine.get_store(target.store_id)
    dist = target.distance_to(source)
    depth = source.depth
    if target.codes[3] == 'T':
        wave = 'any_S'
    elif target.codes[3] == 'Z':
        wave = 'any_P'
    else:
        raise Exception('Channel not supported! Either: "T" or "Z"')

    return store.t(wave, (depth, dist)) + source.time


def get_phase_taperer(engine, source, target, arrival_taper):
    '''
    and taper return :py:class:`CosTaper`
    according to defined arrival_taper times.
    '''
    arrival_time = get_phase_arrival_time(engine, source, target)

    taperer = trace.CosTaper(arrival_time - arrival_taper.a,
                             arrival_time - arrival_taper.b,
                             arrival_time + arrival_taper.c,
                             arrival_time + arrival_taper.d)
    return taperer


def seis_synthetics(engine, sources, targets, arrival_taper=None,
                    filterer=None, reference_taperer=None, plot=False,
                    nprocs=1, outmode='array'):
    '''
    Calculate synthetic seismograms of combination of targets and sources,
    filtering and tapering afterwards (filterer)
    tapering according to arrival_taper around P -or S wave.
    If reference_taper the given taper is always used.
    Returns: Array with data each row-one target
    plot - flag for looking at traces
    '''

    response = engine.process(sources=sources,
                              targets=targets, nprocs=nprocs)

    synt_trcs = []
    for source, target, tr in response.iter_results():
        if arrival_taper is not None:
            if reference_taperer is None:
                taperer = get_phase_taperer(
                    engine,
                    sources[0],
                    target,
                    arrival_taper)
            else:
                taperer = reference_taperer

            # cut traces
            tr.taper(taperer, inplace=True, chop=True)

        if filterer is not None:
            # filter traces
            tr.bandpass(corner_hp=filterer.lower_corner,
                    corner_lp=filterer.upper_corner,
                    order=filterer.order)

        synt_trcs.append(tr)

    if plot:
        trace.snuffle(synt_trcs)

    nt = len(targets)
    ns = len(sources)
    synths = num.vstack([synt_trcs[i].ydata for i in range(len(synt_trcs))])
    tmins = num.vstack([synt_trcs[i].tmin for i in range(nt)]).flatten()

    # stack traces for all sources
    if ns > 1:
        for k in range(ns):
            outstack = num.zeros([nt, synths.shape[1]])
            outstack += synths[(k * nt):(k + 1) * nt, :]
    else:
        outstack = synths

    if outmode == 'traces':
        out = []
        for i in range(nt):
            synt_trcs[i].ydata = outstack[i, :]
            out.append(synt_trcs[i])

        outstack = out

    return outstack, tmins


def taper_filter_traces(data_traces, arrival_taper, filterer, tmins,
                        plot=False):
    '''
    Taper and filter data_traces according to given taper and filterers.
    Tapering will start at the given tmin.
    '''
    cut_traces = []

    for i, tr in enumerate(data_traces):
        cut_trace = tr.copy()
        if arrival_taper is not None:
            taperer = trace.CosTaper(
                float(tmins[i]),
                float(tmins[i] + arrival_taper.b),
                float(tmins[i] + arrival_taper.a + arrival_taper.c),
                float(tmins[i] + arrival_taper.a + arrival_taper.d))

            # taper and cut traces
            cut_trace.taper(taperer, inplace=True, chop=True)

        if filterer is not None:
            # filter traces
            cut_trace.bandpass(corner_hp=filterer.lower_corner,
                               corner_lp=filterer.upper_corner,
                               order=filterer.order)

        cut_traces.append(cut_trace)

        if plot:
            trace.snuffle(cut_traces)

    return num.vstack([cut_traces[i].ydata for i in range(len(data_traces))])
