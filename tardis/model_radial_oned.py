# building of radial_oned_model

import numpy as np
import plasma, packet_source
import logging

import pandas as pd
from pandas.io.pytables import HDFStore
from astropy import constants, units
import montecarlo_multizone
import os
import yaml

import itertools

from .plasma import intensity_black_body

logger = logging.getLogger(__name__)

c = constants.c.cgs.value
h = constants.h.cgs.value
kb = constants.k_B.cgs.value

w_estimator_constant = (c ** 2 / (2 * h)) * (15 / np.pi ** 4) * (h / kb) ** 4 / (4 * np.pi)

synpp_default_yaml_fname = os.path.join(os.path.dirname(__file__), 'data', 'synpp_default.yaml')


class Radial1DModel(object):
    """
        Class to hold the states of the individual shells (the state of the plasma (as a `~plasma.BasePlasma`-object or one of its subclasses),
        , the plasma parameters (e.g. temperature, dilution factor), the dimensions of the shell).


        Parameters
        ----------

        tardis_configuration : `tardis.config_reader.TardisConfiguration`

        velocities : `np.ndarray`
            an array with n+1 (for n shells) velocities (in cm/s) for each of the boundaries (velocities[0] describing
            the inner boundary and velocities[-1] the outer boundary

        densities : `np.ndarray`
            an array with n densities - being the density mid-shell (assumed for the whole shell)

        abundances : `list` or `dict`
            a dictionary for uniform abundances throughout all shells, e.g. dict(Fe=0.5, Si=0.5)
            For a different abundance for each shell list of abundance dictionaries.


        time_explosion : `float`
            time since explosion in seconds

        atom_data : `~tardis.atom_data.AtomData` class or subclass
            Containing the atom data needed for the plasma calculations

        ws : `None` or `list`-like
            ws can only be specified for plasma_type 'nebular'. If `None` is specified at first initialization the class
            calculates an initial geometric dilution factor. When giving a list positive values will be accepted, whereas
            negative values trigger the usage of the geometric calculation

        plasma_type : `str`
            plasma type currently supports 'lte' (using `tardis.plasma.LTEPlasma`)
            or 'nebular' (using `tardis.plasma.NebularPlasma`)

        initial_t_rad : `float`-like or `list`-like
            initial radiative temperature for each shell, if a scalar is specified it initializes with a uniform
            temperature for all shells




    """


    def __init__(self, tardis_config):
        #final preparation for configuration object
        self.tardis_config = tardis_config

        self.atom_data = tardis_config.atom_data

        self.packet_src = packet_source.SimplePacketSource.from_wavelength(tardis_config.spectrum_start,
                                                                           tardis_config.spectrum_end)

        self.no_of_shells = tardis_config.no_of_shells

        #setting time_explosion
        self.time_explosion = tardis_config.time_explosion

        #initializing velocities and radii
        self.v_inner = tardis_config.v_inner
        self.v_outer = tardis_config.v_outer

        self.r_inner = self.v_inner * self.time_explosion
        self.r_outer = self.v_outer * self.time_explosion
        self.r_middle = 0.5 * (self.r_inner + self.r_outer)

        self.volumes = (4. / 3) * np.pi * (self.r_outer ** 3 - self.r_inner ** 3)

        self.mean_densities = tardis_config.mean_densities

        self.t_inner = tardis_config.initial_t_inner

        self.luminosity_outer = tardis_config.luminosity

        self.no_of_packets = tardis_config.no_of_packets
        self.current_no_of_packets = tardis_config.no_of_packets

        self.iterations_max_requested = tardis_config.iterations
        self.iterations_remaining = self.iterations_max_requested - 1
        self.iterations_executed = 0

        self.sigma_thomson = tardis_config.sigma_thomson

        self.spec_nu_bins = np.linspace(tardis_config.spectrum_start_nu.value, tardis_config.spectrum_end_nu.value,
                                        tardis_config.spectrum_bins + 1)
        self.spec_nu = self.spec_nu_bins[:-1]

        self.spec_virtual_flux_nu = np.zeros_like(self.spec_nu)

        self.spec_angstrom = units.Unit('Hz').to('angstrom', self.spec_nu, units.spectral())

        self.spec_flux_angstrom = np.ones_like(self.spec_angstrom)
        self.spec_virtual_flux_angstrom = np.ones_like(self.spec_angstrom)

        self.gui = None
        #reading the convergence criteria
        self.converged = False
        self.convergence_type = tardis_config.convergence_type
        self.t_inner_convergence_parameters = tardis_config.t_inner_convergence_parameters
        self.t_rad_convergence_parameters = tardis_config.t_rad_convergence_parameters
        self.w_convergence_parameters = tardis_config.w_convergence_parameters

        if self.convergence_type == 'specific':
            self.global_convergence_parameters = tardis_config.global_convergence_parameters.copy()



        #Selecting plasma class
        self.plasma_type = tardis_config.plasma_type
        self.radiative_rates_type = tardis_config.radiative_rates_type
        if self.plasma_type == 'lte':
            plasma_class = plasma.LTEPlasma
            if hasattr(tardis_config, 'ws') and tardis_config.ws is not None:
                raise ValueError(
                    "the dilution factor W ('ws') can only be specified when selecting plasma_type='nebular'")

        elif self.plasma_type == 'nebular':
            plasma_class = plasma.NebularPlasma
            if not self.atom_data.has_zeta_data:
                raise ValueError("Requiring Recombination coefficients Zeta for 'nebular' plasma_type")
        else:
            raise ValueError("Currently this model only supports 'lte' or 'nebular'")




        #initializing abundances
        self.abundances = tardis_config.abundances

        self.number_densities = tardis_config.number_densities

        self.selected_atomic_numbers = self.number_densities.columns

        self.line_interaction_type = tardis_config.line_interaction_type


        #setting dilution factors
        if self.plasma_type == 'lte':
            self.ws = np.ones_like(self.r_middle)
        else:
            self.ws = 0.5 * (1 - np.sqrt(1 - self.r_inner[0] ** 2 / self.r_middle ** 2))

        #initializing temperatures

        if np.isscalar(tardis_config.initial_t_rad):
            self.t_rads = np.ones(self.no_of_shells) * tardis_config.initial_t_rad
        else:
            assert len(tardis_config.initial_t_rad) == self.no_of_shells
            self.t_rads = np.array(tardis_config.initial_t_rad, dtype=np.float64)

        self.initialize_plasmas(plasma_class)


    @property
    def electron_densities(self):
        return np.array([plasma.electron_density for plasma in self.plasmas])

    @property
    def line_interaction_type(self):
        return self._line_interaction_type

    @line_interaction_type.setter
    def line_interaction_type(self, value):
        if value == 'scatter':
            self.line_interaction_id = 0
        elif value == 'downbranch':
            self.line_interaction_id = 1
        elif value == 'macroatom':
            self.line_interaction_id = 2
        else:
            raise ValueError('line_interaction_type can only be "scatter", "downbranch", or "macroatom"')

        self._line_interaction_type = value
        #final preparation for atom_data object - currently building data
        self.atom_data.prepare_atom_data(self.selected_atomic_numbers,
                                         line_interaction_type=self.line_interaction_type, max_ion_number=None,
                                         nlte_species=self.tardis_config.nlte_species)


    @property
    def t_inner(self):
        return self._t_inner

    @t_inner.setter
    def t_inner(self, value):
        self._t_inner = value
        self.luminosity_inner = 4 * np.pi * constants.sigma_sb.cgs.value * self.r_inner[0] ** 2 * self._t_inner ** 4
        self.time_of_simulation = 1 / self.luminosity_inner


    def create_packets(self):
        #Energy emitted from the inner boundary
        self.emitted_inner_energy = 4 * np.pi * constants.sigma_sb.cgs.value * self.r_inner[0] ** 2 * (
            self.t_inner) ** 4

        no_of_packets = self.current_no_of_packets
        self.packet_src.create_packets(no_of_packets, self.t_inner)

    def initialize_plasmas(self, plasma_class):
        self.plasmas = []
        self.tau_sobolevs = np.zeros((self.no_of_shells, len(self.atom_data.lines)))
        self.line_list_nu = self.atom_data.lines['nu']

        if self.line_interaction_id in (1, 2):
            if self.line_interaction_id == 1:
                logger.info('Downbranch selected - creating transition probabilities')
            else:
                logger.info('Macroatom selected - creating transition probabilties')
            self.transition_probabilities = []
        else:
            logger.info('Scattering selected - no transition probabilities created')

        for i, ((tmp_index, number_density), current_t_rad, current_w) in \
            enumerate(zip(self.number_densities.iterrows(), self.t_rads, self.ws)):

            logger.debug('Initializing Shell %d Plasma with T=%.3f W=%.4f' % (i, current_t_rad, current_w))
            if self.radiative_rates_type in ('lte',):
                j_blues = plasma.intensity_black_body(self.atom_data.lines.nu.values, current_t_rad)
            elif self.radiative_rates_type in ('nebular', 'detailed'):
                j_blues = current_w * plasma.intensity_black_body(self.atom_data.lines.nu.values, current_t_rad)
            else:
                raise ValueError('For the current plasma_type (%s) the radiative_rates_type can only'
                                 ' be "lte" or "detailed" or "nebular"' % (self.plasma_type))

            current_plasma = plasma_class(t_rad=current_t_rad, w=current_w, number_density=number_density,
                                          atom_data=self.atom_data, time_explosion=self.time_explosion,
                                          nlte_species=self.tardis_config.nlte_species,
                                          nlte_options=self.tardis_config.nlte_options, zone_id=i, j_blues=j_blues)

            self.tau_sobolevs[i] = current_plasma.tau_sobolevs

            self.plasmas.append(current_plasma)

        self.tau_sobolevs = np.array(self.tau_sobolevs, dtype=float)
        self.j_blues = np.zeros_like(self.tau_sobolevs)

        if self.line_interaction_id in (1, 2):
            self.calculate_transition_probabilities()

            # update plasmas

    def calculate_transition_probabilities(self):
        self.transition_probabilities = []

        for current_plasma in self.plasmas:
            self.transition_probabilities.append(current_plasma.calculate_transition_probabilities().values)

        self.transition_probabilities = np.array(self.transition_probabilities, dtype=np.float64)

    def calculate_updated_radiationfield(self, nubar_estimator, j_estimator):
        """
        Calculate an updated radiation field from the :math:`\\bar{nu}_\\textrm{estimator}` and :math:`\\J_\\textrm{estimator}`
        calculated in the montecarlo simulation. The details of the calculation can be found in the documentation.

        Parameters
        ----------

        nubar_estimator : ~np.ndarray (float)

        j_estimator : ~np.ndarray (float)

        Returns
        -------

        updated_t_rads : ~np.ndarray (float)

        updated_ws : ~np.ndarray (float)

        """
        trad_estimator_constant = 0.260944706 * h / kb # (pi**4 / 15)/ (24*zeta(5))

        updated_t_rads = trad_estimator_constant * nubar_estimator / j_estimator
        updated_ws = j_estimator / (
            4 * constants.sigma_sb.cgs.value * updated_t_rads ** 4 * self.time_of_simulation * self.volumes)

        return updated_t_rads, updated_ws

    def normalize_j_blues(self):
        norm_factor = (constants.c.cgs.value * self.time_explosion /
                       (4 * np.pi * self.time_of_simulation * self.volumes)).reshape((self.volumes.shape[0], 1))
        self.j_blues *= norm_factor
        for i, current_j_blue in enumerate(self.j_blues):
            nus = self.atom_data.lines.nu[current_j_blue == 0.0].values
            self.j_blues[i][self.j_blues[i] == 0.0] = self.tardis_config.w_epsilon * intensity_black_body(nus,
                                                                                                          self.plasmas[
                                                                                                              i].t_rad)

    def update_plasmas(self):
        for i, (current_plasma, new_trad, new_ws) in enumerate(zip(self.plasmas, self.t_rads, self.ws)):
            logger.debug('Updating Shell %d Plasma with T=%.3f W=%.4f' % (i, new_trad, new_ws))
            if self.radiative_rates_type == 'lte':
                j_blues = plasma.intensity_black_body(self.atom_data.lines.nu.values, new_trad)
            elif self.radiative_rates_type == 'nebular':
                j_blues = new_ws * plasma.intensity_black_body(self.atom_data.lines.nu.values, new_trad)

            elif self.radiative_rates_type == 'detailed':
                j_blues = self.j_blues[i]
            else:
                raise ValueError('For the current plasma_type (%s) the radiative_rates_type can only'
                                 ' be "lte" or "detailed" or "nebular"' % (self.plasma_type))

            current_plasma.set_j_blues(j_blues)
            if self.plasma_type == 'lte':
                new_ws = 1.0
            current_plasma.update_radiationfield(new_trad, w=new_ws)
            self.tau_sobolevs[i] = current_plasma.tau_sobolevs

        if self.line_interaction_id in (1, 2):
            self.calculate_transition_probabilities()


    def calculate_spectrum(self):

        if self.tardis_config.sn_distance is None:
            logger.info('Distance to supernova not selected assuming 10 pc for calculation of spectra')
            distance = units.Quantity(10, 'pc').to('cm').value
        else:
            distance = self.tardis_config.sn_distance
        self.spec_flux_nu = np.histogram(self.montecarlo_nu[self.montecarlo_nu > 0],
                                         weights=self.montecarlo_energies[self.montecarlo_energies > 0],
                                         bins=self.spec_nu_bins)[0]

        flux_scale = (self.time_of_simulation * (self.spec_nu[1] - self.spec_nu[0]) * (4 * np.pi * distance ** 2))

        self.spec_flux_nu /= flux_scale

        self.spec_virtual_flux_nu /= flux_scale

        self.spec_reabsorbed_nu = \
            np.histogram(self.montecarlo_nu[self.montecarlo_nu < 0],
                         weights=self.montecarlo_energies[self.montecarlo_nu < 0], bins=self.spec_nu_bins)[0]
        self.spec_reabsorbed_nu /= flux_scale

        self.spec_angstrom = units.Unit('Hz').to('angstrom', self.spec_nu, units.spectral())

        self.spec_flux_angstrom = (self.spec_flux_nu * self.spec_nu ** 2 / constants.c.cgs.value / 1e8)
        self.spec_reabsorbed_angstrom = (self.spec_reabsorbed_nu * self.spec_nu ** 2 / constants.c.cgs.value / 1e8)
        self.spec_virtual_flux_angstrom = (self.spec_virtual_flux_nu * self.spec_nu ** 2 / constants.c.cgs.value / 1e8)


    def simulate(self, update_radiation_field=True, enable_virtual=False):
        self.create_packets()
        self.spec_virtual_flux_nu[:] = 0.0

        if enable_virtual:
            no_of_virtual_packets = self.tardis_config.no_of_virtual_packets
        else:
            no_of_virtual_packets = 0

        self.montecarlo_nu, self.montecarlo_energies, self.j_estimators, self.nubar_estimators, \
        last_line_interaction_in_id, last_line_interaction_out_id, \
        self.last_interaction_type, self.last_line_interaction_shell_id = \
            montecarlo_multizone.montecarlo_radial1d(self,
                                                     virtual_packet_flag=no_of_virtual_packets)

        self.normalize_j_blues()

        self.calculate_spectrum()
        self.last_line_interaction_in_id = self.atom_data.lines_index.index.values[last_line_interaction_in_id]
        self.last_line_interaction_in_id[last_line_interaction_in_id == -1] = -1
        self.last_line_interaction_out_id = self.atom_data.lines_index.index.values[last_line_interaction_out_id]
        self.last_line_interaction_out_id[last_line_interaction_out_id == -1] = -1

        self.iterations_executed += 1
        self.iterations_remaining -= 1

        if self.gui is not None:
            self.gui.update_data(self)
            self.gui.show()

        if update_radiation_field:
            self.update_radiationfield()
            self.update_plasmas()


    def update_radiationfield(self, log_sampling=5):
        """
        Updating radiation field
        """

        updated_t_rads, updated_ws = self.calculate_updated_radiationfield(self.nubar_estimators, self.j_estimators)
        old_t_rads = self.t_rads.copy()
        old_ws = self.ws.copy()
        old_t_inner = self.t_inner

        emitted_energy = self.emitted_inner_energy * \
                         np.sum(self.montecarlo_energies[self.montecarlo_energies >= 0]) / 1.
        absorbed_energy = self.emitted_inner_energy * \
                          np.sum(self.montecarlo_energies[self.montecarlo_energies < 0]) / -1.
        updated_t_inner = self.t_inner * (emitted_energy / self.luminosity_outer) ** -.25

        convergence_t_rads = abs(old_t_rads - updated_t_rads) / updated_t_rads
        convergence_ws = abs(old_ws - updated_ws) / updated_ws
        convergence_t_inner = abs(old_t_inner - updated_t_inner) / updated_t_inner

        if self.convergence_type == 'damped':
            self.t_rads += self.t_rad_convergence_parameters['damping_constant'] * (updated_t_rads - self.t_rads)
            self.ws += self.w_convergence_parameters['damping_constant'] * (updated_ws - self.ws)
            self.t_inner += self.w_convergence_parameters['damping_constant'] * (updated_t_inner - self.t_inner)

        elif self.convergence_type == 'specific':
            self.t_rads += self.t_rad_convergence_parameters['damping_constant'] * (updated_t_rads - self.t_rads)
            self.ws += self.w_convergence_parameters['damping_constant'] * (updated_ws - self.ws)
            self.t_inner += self.t_inner_convergence_parameters['damping_constant'] * (updated_t_inner - self.t_inner)

            t_rad_converged = (float(np.sum(convergence_t_rads < self.t_rad_convergence_parameters['threshold'])) \
                               / self.no_of_shells) > self.t_rad_convergence_parameters['fraction']

            w_converged = (float(np.sum(convergence_t_rads < self.t_rad_convergence_parameters['threshold'])) \
                           / self.no_of_shells) > self.t_rad_convergence_parameters['fraction']

            t_inner_converged = convergence_t_inner < self.t_rad_convergence_parameters['threshold']

            if t_rad_converged and t_inner_converged and w_converged:
                if not self.converged:
                    self.converged = True
                    self.iterations_remaining = self.global_convergence_parameters['hold']

            else:
                if self.converged:
                    self.iterations_remaining = self.iterations_max_requested - self.iterations_executed
                    self.converged = False

        self.temperature_logging = pd.DataFrame(
            {'t_rads': old_t_rads, 'updated_t_rads': updated_t_rads, 'converged_t_rads': convergence_t_rads,
             'new_trads': self.t_rads, 'ws': old_ws, 'updated_ws': updated_ws, 'converged_ws': convergence_ws,
             'new_ws': self.ws})
        self.temperature_logging.index.name = 'Shell'

        temperature_logging = str(self.temperature_logging[::log_sampling])

        temperature_logging = ''.join(['\t%s\n' % item for item in temperature_logging.split('\n')])

        logger.info('Plasma stratification:\n%s\n', temperature_logging)
        logger.info("Luminosity emitted = %.5e Luminosity absorbed = %.5e Luminosity requested = %.5e", emitted_energy,
                    absorbed_energy,
                    self.luminosity_outer)
        logger.info('Calculating new t_inner = %.3f', updated_t_inner)


    def create_synpp_yaml(self, fname, lines_db=None):
        logger.warning('Currently only works with Si and a special setup')
        if not self.atom_data.has_synpp_refs:
            raise ValueError(
                'The current atom dataset does not contain the necesarry reference files (please contact the authors)')

        self.atom_data.synpp_refs['ref_log_tau'] = -99.0
        for key, value in self.atom_data.synpp_refs.iterrows():
            try:
                tau_sobolev_idx = self.atom_data.lines_index.ix[value['line_id']]
            except KeyError:
                continue

            self.atom_data.synpp_refs['ref_log_tau'].ix[key] = np.log10(self.plasmas[0].tau_sobolevs[tau_sobolev_idx])

        relevant_synpp_refs = self.atom_data.synpp_refs[self.atom_data.synpp_refs['ref_log_tau'] > -50]

        yaml_reference = yaml.load(file(synpp_default_yaml_fname))

        if lines_db is not None:
            yaml_reference['opacity']['line_dir'] = os.path.join(lines_db, 'lines')
            yaml_reference['opacity']['line_dir'] = os.path.join(lines_db, 'refs.dat')

        yaml_reference['output']['min_wl'] = float(self.spec_angstrom.min())
        yaml_reference['output']['max_wl'] = float(self.spec_angstrom.max())

        yaml_reference['opacity']['v_ref'] = float(self.v_inner[0] / 1e8)
        yaml_reference['grid']['v_outer_max'] = float(self.v_outer[-1] / 1e8)

        #pdb.set_trace()

        yaml_setup = yaml_reference['setups'][0]
        yaml_setup['ions'] = []
        yaml_setup['log_tau'] = []
        yaml_setup['active'] = []
        yaml_setup['temp'] = []
        yaml_setup['v_min'] = []
        yaml_setup['v_max'] = []
        yaml_setup['aux'] = []

        for species, synpp_ref in relevant_synpp_refs.iterrows():
            yaml_setup['ions'].append(100 * species[0] + species[1])
            yaml_setup['log_tau'].append(float(synpp_ref['ref_log_tau']))
            yaml_setup['active'].append(True)
            yaml_setup['temp'].append(yaml_setup['t_phot'])
            yaml_setup['v_min'].append(yaml_reference['opacity']['v_ref'])
            yaml_setup['v_max'].append(yaml_reference['grid']['v_outer_max'])
            yaml_setup['aux'].append(1e200)

        yaml.dump(yaml_reference, stream=file(fname, 'w'), explicit_start=True)

    def to_hdf5(self, buffer_or_fname, path=''):
        if isinstance(buffer_or_fname, basestring):
            hdf_store = pd.HDFStore(buffer_or_fname)
        elif isinstance(buffer_or_fname, pd.HDFStore):
            hdf_store = buffer_or_fname
        else:
            raise IOError('Please specify either a filename or an HDFStore')

        for i, plasma in enumerate(self.plasmas):
            plasma.to_hdf5(hdf_store, os.path.join(path, 'plasma%d' % i))

        t_rads_path = os.path.join(path, 't_rads')
        pd.Series(self.t_rads).to_hdf(hdf_store, t_rads_path)

        ws_path = os.path.join(path, 'ws')
        pd.Series(self.ws).to_hdf(hdf_store, ws_path)

        electron_densities_path = os.path.join(path, 'electron_densities')
        pd.Series(self.electron_densities).to_hdf(hdf_store, electron_densities_path)

        last_line_interaction_in_id_path = os.path.join(path, 'last_line_interaction_in_id')
        pd.Series(self.last_line_interaction_in_id).to_hdf(hdf_store, last_line_interaction_in_id_path)

        last_line_interaction_out_id_path = os.path.join(path, 'last_line_interaction_out_id')
        pd.Series(self.last_line_interaction_out_id).to_hdf(hdf_store, last_line_interaction_out_id_path)

        spectrum = pd.DataFrame.from_dict(dict(wave=self.spec_angstrom, flux=self.spec_flux_angstrom))
        spectrum.to_hdf(hdf_store, os.path.join(path, 'spectrum'))

        spectrum_virtual = pd.DataFrame.from_dict(dict(wave=self.spec_angstrom, flux=self.spec_virtual_flux_angstrom))
        spectrum_virtual.to_hdf(hdf_store, os.path.join(path, 'spectrum_virtual'))

        hdf_store.flush()
        return hdf_store


    def plot_spectrum(self, ax, mode='wavelength', virtual=True):
        if mode == 'wavelength':
            x = self.spec_angstrom
            if virtual:
                y = self.spec_virtual_flux_angstrom
            else:
                y = self.spec_flux_angstrom
            xlabel = 'Wavelength [\AA]'
            if self.tardis_config.lum_density:
                ylabel = 'Flux [erg s^-1 cm^-2 \AA^-1]'
            else:
                ylabel = 'Flux [erg s^-1 \AA^-1]'

        ax.plot(x, y)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)

    def save_spectrum(self, prefix):
        np.savetxt(prefix + '_virtual_spec.dat', zip(self.spec_angstrom, self.spec_virtual_flux_angstrom))
        np.savetxt(prefix + '_spec.dat', zip(self.spec_angstrom, self.spec_flux_angstrom))


class ModelHistory(object):
    """
    Records the history of the model
    """
    _store_attributes = ['t_rads', 'ws', 'electron_density', 'j_blues', 'tau_sobolevs']

    @classmethod
    def from_hdf5(cls, fname):
        history_store = HDFStore(fname)
        for attribute in cls._store_attributes:
            setattr(cls, attribute, history_store[attribute])
        history_store.close()

    @classmethod
    def from_tardis_config(cls, tardis_config, store_t_rads=False, store_ws=False, store_convergence=False,
                           store_electron_density=False,
                           store_level_populations=False, store_j_blues=False, store_tau_sobolevs=False,
                           store_t_inner=False):
        history = cls()
        cls.store_t_rads = store_t_rads
        cls.store_ws = store_ws
        cls.store_electron_density = store_electron_density
        cls.store_level_populations = store_level_populations
        cls.store_j_blues = store_j_blues
        cls.store_tau_sobolves = store_tau_sobolevs
        cls.store_convergence = store_convergence
        cls.store_t_inner = store_t_inner

        if store_t_rads:
            history.t_rads = pd.DataFrame(index=np.arange(tardis_config.no_of_shells))
        if store_ws:
            history.ws = pd.DataFrame(index=np.arange(tardis_config.no_of_shells))
        if store_electron_density:
            history.electron_density = pd.DataFrame(index=np.arange(tardis_config.no_of_shells))
        if store_level_populations:
            history.level_populations = {}
        if store_j_blues:
            history.j_blues = {}
        if store_tau_sobolevs:
            history.tau_sobolevs = {}

        if store_convergence:
            history.convergence_panel = {}

        if store_t_inner:
            history.t_inner = []

        history.iteration_counter = itertools.count()

        return history


    def store(self, radial1d_mdl):
        iteration = self.iteration_counter.next()
        if self.store_t_rads:
            self.t_rads['iter%03d' % iteration] = radial1d_mdl.t_rads
        if self.store_ws:
            self.ws['iter%03d' % iteration] = radial1d_mdl.ws

        if self.store_t_inner:
            self.t_inner.append(radial1d_mdl.t_inner)
        if self.store_electron_density:
            self.electron_density['iter%03d' % iteration] = radial1d_mdl.electron_density

        if self.store_level_populations:
            current_level_populations = pd.DataFrame(index=radial1d_mdl.atom_data.levels.index)
        if self.store_j_blues:
            current_j_blues = pd.DataFrame(index=radial1d_mdl.atom_data.lines.index)
        if self.store_tau_sobolves:
            current_tau_sobolevs = pd.DataFrame(index=radial1d_mdl.atom_data.lines.index)
        for i, plasma in enumerate(radial1d_mdl.plasmas):
            if self.store_level_populations:
                current_level_populations[i] = plasma.level_populations
            if self.store_j_blues:
                current_j_blues[i] = plasma.j_blues
            if self.store_tau_sobolves:
                current_tau_sobolevs[i] = plasma.tau_sobolevs
        if self.store_level_populations:
            self.level_populations['iter%03d' % iteration] = current_level_populations.copy()
        if self.store_j_blues:
            self.j_blues['iter%03d' % iteration] = current_j_blues.copy()
        if self.store_tau_sobolves:
            self.tau_sobolevs['iter%03d' % iteration] = current_tau_sobolevs.copy()
        if self.store_convergence:
            self.convergence_panel['iter%03d' % iteration] = radial1d_mdl.temperature_logging.copy()

    def finalize(self):
        if self.store_level_populations:
            self.level_populations = pd.Panel.from_dict(self.level_populations)
        if self.store_j_blues:
            self.j_blues = pd.Panel.from_dict(self.j_blues)
        if self.store_tau_sobolves:
            self.tau_sobolevs = pd.Panel.from_dict(self.tau_sobolevs)

        if self.store_convergence:
            self.convergence_panel = pd.Panel.from_dict(self.convergence_panel)

    def to_hdf5(self, fname, complevel=9, complib='bzip2'):
        if os.path.exists(fname):
            logger.warning('Overwrite %s with current history', fname)
        history_store = HDFStore(fname, mode='w', complevel=complevel, complib=complib)
        for attribute in self._store_attributes:
            history_store[attribute] = getattr(self, attribute)
        history_store.close()


