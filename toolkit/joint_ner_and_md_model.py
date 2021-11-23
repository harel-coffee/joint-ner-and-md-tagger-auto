import os, sys
import re
import numpy as np

import dynet
from dynet import Model, BiRNNBuilder, CoupledLSTMBuilder

import codecs
import pickle

import logging

from toolkit.crf import CRF
from utils.dynetsaver import DynetSaver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from utils import get_name, create_a_model_subpath, add_a_model_path_to_the_model_paths_database


def log_sum_exp(scores):
    npval = scores.npvalue()
    argmax_score = np.argmax(npval)
    max_score_expr = dynet.pick(scores, argmax_score)
    max_score_expr_broadcast = dynet.concatenate([max_score_expr] * (npval.shape[0]))
    return max_score_expr + dynet.log(
        dynet.sum_dim(dynet.transpose(dynet.exp(scores - max_score_expr_broadcast)), [1]))


class MainTaggerModel(object):
    """
    Network architecture.
    """
    id_to_word = {} # type: dict
    id_to_char = {} # type: dict
    id_to_tag = {}  # type: dict
    id_to_morpho_tag = {} # type: dict

    saver = None  # type: DynetSaver

    def __init__(self, opts=None, parameters=None, models_path=None, model_path=None, model_epoch_dir_path=None,
                 overwrite_mappings=0):
        """
        Initialize the model. We either provide the parameters and a path where
        we store the models, or the location of a trained model.
        """

        self.entity_types = []

        self._valid_path_cache = dict()

        self.training = True
        self.n_bests = 0
        self.overwrite_mappings = overwrite_mappings

        self.model = Model()

        if model_path is None:
            assert parameters and models_path and opts
            # Create a name based on the parameters
            self.parameters = parameters
            self.opts = opts
            self.name = get_name(parameters)
            # MainTaggerModel location
            # MainTaggerModel location
            available_model_subpath, model_path_id = create_a_model_subpath(models_path)
            # model_path = os.path.join(models_path, available_model_subpath)
            add_a_model_path_to_the_model_paths_database(models_path, available_model_subpath,
                                                         get_name(parameters))
            self.model_path = available_model_subpath
            # model_path = os.path.join(models_path, self.name)
            # self.model_path = model_path
            self.parameters_path = os.path.join(self.model_path, 'parameters.pkl')
            self.mappings_path = os.path.join(self.model_path, 'mappings.pkl')
            self.opts_path = os.path.join(self.model_path, 'opts.pkl')
            # Create directory for the model if it does not exist
            if not os.path.exists(self.model_path):
                os.makedirs(self.model_path)
            # Save the parameters to disk
            with open(self.parameters_path, 'wb') as f:
                pickle.dump(parameters, f)
            # Save the command line parameters to disk
            with open(self.opts_path, 'wb') as f:
                pickle.dump(opts, f)
        else:
            assert parameters is None and opts is None and models_path and model_path and model_epoch_dir_path
            # MainTaggerModel location
            self.model_path = model_path
            self.parameters_path = os.path.join(models_path, model_path, 'parameters.pkl')
            self.mappings_path = os.path.join(models_path, model_path, 'mappings.pkl')
            self.opts_path = os.path.join(models_path, model_path, 'opts.pkl')
            # Load the parameters and the mappings from disk
            with open(self.parameters_path, 'rb') as f:
                self.parameters = pickle.load(f)
            # Read opts from the saved file
            with open(self.opts_path, 'rb') as f:
                self.opts = pickle.load(f)
            self.reload_mappings()

        self.components = {}

    def save_mappings(self, id_to_word, id_to_char, id_to_tag, id_to_morpho_tag):
        """
        We need to save the mappings if we want to use the model later.
        """
        self.id_to_word = id_to_word
        self.id_to_char = id_to_char
        self.id_to_tag = id_to_tag
        self.id_to_morpho_tag = id_to_morpho_tag

        if os.path.exists(self.mappings_path) and not self.overwrite_mappings:
            print("Aborting. A previous mappings file exists. You should explicitly state to overwrite the mappings file")
            sys.exit(1)
        else:
            with open(self.mappings_path, 'wb') as f:
                mappings = {
                    'id_to_word': self.id_to_word,
                    'id_to_char': self.id_to_char,
                    'id_to_tag': self.id_to_tag,
                    'id_to_morpho_tag': self.id_to_morpho_tag,
                }
                pickle.dump(mappings, f)

    def reload_mappings(self):
        """
        Load mappings from disk.
        """
        with open(self.mappings_path, 'rb') as f:
            mappings = pickle.load(f)
        self.id_to_word = mappings['id_to_word']
        self.id_to_char = mappings['id_to_char']
        self.id_to_tag = mappings['id_to_tag']
        self.id_to_morpho_tag = mappings['id_to_morpho_tag']

    def add_component(self, param):
        """
        Add a new parameter to the network.
        """
        if param.name in self.components:
            raise Exception('The network already has a parameter "%s"!'
                            % param.name)
        self.components[param.name] = param

    def save(self, epoch=None, best_performances=[], epoch_costs=[]):
        """
        Write components values to disk.
        """

        # for name, param in self.components.items():
        #     param_path = os.path.join(path, "%s.mat" % name)
        #     if hasattr(param, 'params'):
        #         param_values = {p.name: p.get_value() for p in param.params}
        #     else:
        #         param_values = {name: param.get_value()}
        #     scipy.io.savemat(param_path, param_values)

        self.saver.save(epoch=epoch, n_bests=self.n_bests)

        self.save_best_performances_and_costs(epoch, best_performances, epoch_costs)

    def save_best_performances_and_costs(self, epoch, best_performances, epoch_costs):

        path = self.model_path
        model_ckpt_filename = ("model-epoch-%08d" % epoch) if epoch is not None else (
        "best-models-%08d" % self.n_bests)
        if len(best_performances) > 0:
            best_performances_path = os.path.join(path,
                                                  "%s-%s.txt" % (
                                                  model_ckpt_filename, "best_performances"))
            best_performances_f = open(best_performances_path, "w")
            best_performances_f.write(" ".join([str(b) for b in best_performances]) + "\n")
            best_performances_f.close()
        if len(epoch_costs) > 0:
            epoch_cost_path = os.path.join(path, "%s-%s.txt" % (model_ckpt_filename, "epoch_cost"))
            epoch_cost_f = open(epoch_cost_path, "w")
            epoch_cost_f.write(" ".join([str(e) for e in epoch_costs]) + "\n")
            epoch_cost_f.write(str(np.mean(epoch_costs)) + "\n")
            epoch_cost_f.close()

    def reload(self, model_epoch_dir_path=None):
        """
        Load components values from disk.
        """
        path = self.model_path
        if model_epoch_dir_path:
            path = model_epoch_dir_path
        else:
            path = os.path.join(path, "epoch-%08d" % 0)
        print(path)
        assert os.path.exists(path)

        self.saver.restore(os.path.join(path, "model.ckpt"))

    def get_last_layer_context_representations(self, sentence,
                                               context_representations_for_crf_loss,
                                               context_representations_for_md_loss):
        last_layer_context_representations = context_representations_for_crf_loss

        if self.parameters['active_models'] in [1, 2, 3]:

            if self.parameters['active_models'] == 1 and \
                   self.parameters['integration_mode'] != 0:
                assert False, "integration_mode should be set to zero when active_models == 1"

            if self.parameters['debug'] == 1:
                print(("str_words", sentence["str_words"]))
            morph_analysis_representations, morph_analysis_scores = \
                self.get_morph_analysis_representations_and_scores(sentence,
                                                                   context_representations_for_md_loss)

            selected_morph_analysis_representations = \
                self.disambiguate_morph_analyzes(morph_analysis_scores)

            if 'golden_morph_analysis_indices' in list(sentence.keys()):
                md_loss = dynet.esum(
                    [dynet.pickneglogsoftmax(morph_analysis_scores_for_word, golden_idx)
                     for golden_idx, morph_analysis_scores_for_word in
                     zip(sentence['golden_morph_analysis_indices'],
                         morph_analysis_scores)])
            else:
                md_loss = dynet.scalarInput(0)

            if self.parameters['integration_mode'] == 2:
                # on the other hand, we can implement two layer of contexts, which we use the
                # first for morphological disambiguation and then concatenate the predicted/computed/
                # selected morphological analysis representation to use for calculating tag_scores
                last_layer_context_representations = \
                    [dynet.concatenate([context,
                                        morph_analysis_representations[word_pos]
                                        [selected_morph_analysis_representation_pos]])
                     for word_pos, (selected_morph_analysis_representation_pos, context) in
                     enumerate(
                         zip(selected_morph_analysis_representations, context_representations_for_crf_loss))]
            if md_loss.value() > 1000:
                logging.error("BEEP")
        else:
            # only the plain old NER model
            # we must decide whether we should implement the morphological embeddings scheme here.
            md_loss = dynet.scalarInput(0)
            selected_morph_analysis_representations = None
            last_layer_context_representations = context_representations_for_crf_loss

        assert last_layer_context_representations is not None
        return last_layer_context_representations, md_loss, selected_morph_analysis_representations

    def get_morph_analysis_scores(self, morph_analysis_representations, context_representations):

        # (10) and (11) in Shen et al. "The Role of Context ..."
        def transform_context(context):
            return dynet.tanh(dynet.affine_transform([self.transform_context_layer_b.expr(),
                                                      self.transform_context_layer_W.expr(),
                                                      context]))
            #return dynet.tanh(dynet.sum_cols(dynet.reshape(context, (int(self.sentence_level_bilstm_contexts_length/2), 2))))

        if self.parameters['debug'] == 1:
            print(("morph_analysis_representations", morph_analysis_representations))
            print(("context_representations", context_representations))

        morph_analysis_scores = \
            [dynet.softmax(
                dynet.concatenate([dynet.dot_product(morph_analysis_representation,
                                                     transform_context(context)) # sum + tanh for context[:half] and contet[half:]
                                   for morph_analysis_representation in
                                   morph_analysis_representations[word_pos]]))
                for word_pos, context in enumerate(context_representations)]
        return morph_analysis_scores

    def get_morph_analysis_representations_and_scores(self, sentence, context_representations):

        morph_analysis_representations = self.get_morph_analysis_representations(sentence)

        morph_analysis_scores = self.get_morph_analysis_scores(morph_analysis_representations,
                                                               context_representations)

        return morph_analysis_representations, morph_analysis_scores

    def disambiguate_morph_analyzes(self, morph_analysis_scores):

        selected_morph_analysis_representations = [
            np.argmax(morph_analysis_scores_for_word.npvalue())
            for morph_analysis_scores_for_word in morph_analysis_scores]

        return selected_morph_analysis_representations

    def build(self,
              char_dim,
              char_lstm_dim,
              ch_b,
              mt_d,
              word_dim,
              word_lstm_dim,
              w_b,
              lr_method,
              pre_emb,
              cap_dim,
              training=True,
              **kwargs
              ):
        """
        Build the network.
        """

        self.training = training

        def _create_get_representation(activation_function=lambda x: x):
            """
            Helper function to create a function which assembles a representation given an
            activation_function
            :param activation_function: 
            :return: 
            """
            def f(obj, es):
                representations = []
                # for e in es:
                #     dynet.ensure_freshness(e)
                for (fb, bb) in obj.builder_layers:
                    fs = fb.initial_state().transduce(es)
                    bs = bb.initial_state().transduce(reversed(es))
                    es = [dynet.concatenate([f, b]) for f, b in zip(fs, reversed(bs))]
                    representations.append(activation_function(dynet.concatenate([fs[-1], bs[-1]])))
                return representations
            return f

        BiRNNBuilder.get_representation = _create_get_representation(activation_function=dynet.rectify)
        BiRNNBuilder.get_representation_concat = _create_get_representation()


        # Training parameters
        n_words = len(self.id_to_word)
        n_chars = len(self.id_to_char)
        n_tags = len(self.id_to_tag)
        n_morpho_tags = len(self.id_to_morpho_tag)

        # self.entity_types = set([t.replace("B-", "") for t in self.id_to_tag.values() if t.startswith("B-")])
        self.entity_types = sorted(list(set([t.replace("B-", "").replace("S-", "") for t in self.id_to_tag.values() if
                                (t.startswith("B-") or t.startswith("S-"))])))

        # Number of capitalization features
        if cap_dim:
            n_cap = 17

        # Final input (all word features)
        word_representation_dim = 0

        def get_scale(shape):
            return np.sqrt(6/np.sum(list(shape)))

        #
        # Word inputs
        #
        if word_dim:
            # Initialize with pretrained embeddings
            scale = get_scale((n_words, word_dim))
            new_weights = scale * np.random.uniform(-1.0, 1.0, (n_words, word_dim))
            # new_weights = np.zeros([n_words, word_dim], dtype='float32')
            if pre_emb and training:
                print('Loading pretrained embeddings from %s...' % pre_emb)
                pretrained = {}
                emb_invalid = 0
                for i, line in enumerate(codecs.open(pre_emb, 'r', 'utf-8')):
                    line = line.split()
                    if len(line) == word_dim + 1:
                        pretrained[line[0]] = np.array(
                            [float(x) for x in line[1:]]
                        ).astype(np.float32)
                    else:
                        emb_invalid += 1
                if emb_invalid > 0:
                    print('WARNING: %i invalid lines' % emb_invalid)
                c_found = 0
                c_lower = 0
                c_zeros = 0
                # Lookup table initialization
                for i in range(n_words):
                    raw_word = self.id_to_word[i]
                    if raw_word != "<UNK>":
                        # word = raw_word.split(" ")[1]
                        word = raw_word
                    else:
                        word = raw_word
                    # print word
                    if word in pretrained:
                        new_weights[i] = pretrained[word]
                        c_found += 1
                    elif word.lower() in pretrained:
                        new_weights[i] = pretrained[word.lower()]
                        c_lower += 1
                    elif re.sub('\d', '0', word.lower()) in pretrained:
                        new_weights[i] = pretrained[
                            re.sub('\d', '0', word.lower())
                        ]
                        c_zeros += 1

                print('Loaded %i pretrained embeddings.' % len(pretrained))
                print(('%i / %i (%.4f%%) words have been initialized with '
                       'pretrained embeddings.') % (
                          c_found + c_lower + c_zeros, n_words,
                          100. * (c_found + c_lower + c_zeros) / n_words
                      ))
                print(('%i found directly, %i after lowercasing, '
                       '%i after lowercasing + zero.') % (
                          c_found, c_lower, c_zeros
                      ))
            word_representation_dim += word_dim
            self.word_embeddings = self.model.lookup_parameters_from_numpy(new_weights, name="wordembeddings")
            # self.word_embeddings = self.model.add_lookup_parameters((n_words, word_dim),
            #                                                         init=dynet.NumpyInitializer(
            #                                                             new_weights),
            #                                                         name="wordembeddings")


        def create_bilstm_layer(label, input_dim, lstm_dim, bilstm=True):
            if bilstm:
                builder = BiRNNBuilder(1, input_dim, lstm_dim, self.model, CoupledLSTMBuilder)
            else:
                builder = CoupledLSTMBuilder(1, input_dim, lstm_dim, self.model)

            return builder

        # Chars inputs
        #
        if char_dim:
            self.char_embeddings = self.model.add_lookup_parameters((n_chars, char_dim),
                                                                    name="charembeddings")

            self.char_lstm_layer = create_bilstm_layer("char",
                                                       char_dim,
                                                       (2 if ch_b else 1) * char_lstm_dim,
                                                       bilstm=True if ch_b else False)

            word_representation_dim += (2 if ch_b else 1) * char_lstm_dim

        # if self.parameters['integration_mode'] in [1, 2] or self.parameters['active_models'] in [1,
        #                                                                                          2,
        #                                                                                          3]:
        if self.parameters['active_models'] in [1, 2, 3]:

            self.char_lstm_layer_for_morph_analysis_roots = \
                create_bilstm_layer("char_for_morph_analysis_root",
                                   char_dim,
                                   2 * mt_d,
                                   bilstm=True)

            self.morpho_tag_embeddings = self.model.add_lookup_parameters((n_morpho_tags, mt_d),
                                                                    name="charembeddings")
            # self.blank_morpho_tag_embedding = self.model.add_parameters(mt_d)
            self.blank_morpho_tag_embedding = dynet.inputVector(list(np.zeros(mt_d)))
            self.morpho_tag_lstm_layer_for_morph_analysis_tags = \
                create_bilstm_layer("morpho_tag_for_morph_analysis_tags",
                                    mt_d,
                                    2 * mt_d,
                                    bilstm=True)


            # self.blank_morpho_tag_sequence_rep = \
            #     self.morpho_tag_lstm_layer_for_morph_analysis_tags.get_representation([self.blank_morpho_tag_embedding])[0]

        if self.parameters['use_golden_morpho_analysis_in_word_representation']:

            assert self.parameters['integration_mode'] == 0 and \
                   self.parameters['active_models'] == 0, "This feature is meaningful if we solely aim NER task."

            self.morpho_tag_embeddings = self.model.add_lookup_parameters((n_morpho_tags, mt_d),
                                                                              name="charembeddings")

            self.old_style_morpho_tag_lstm_layer_for_golden_morpho_analyzes = \
                create_bilstm_layer("old_style_morpho_tag_lstm_layer_for_golden_morpho_analyzes",
                                    mt_d,
                                    2 * mt_d,
                                    bilstm=True)

            word_representation_dim += 2 * mt_d

        #
        # Capitalization feature
        #
        if cap_dim:
            word_representation_dim += cap_dim
            self.cap_embeddings = self.model.add_lookup_parameters((n_cap, cap_dim),
                                                                   name="capembeddings")

        if self.parameters['multilayer'] and self.parameters['shortcut_connections']:
            shortcut_connection_addition = word_representation_dim
            self.sentence_level_bilstm_contexts_length = shortcut_connection_addition + 2 * word_lstm_dim
        else:
            self.sentence_level_bilstm_contexts_length = 2 * word_lstm_dim
        # else:
        #     self.sentence_level_bilstm_contexts_length = word_lstm_dim # TODO: Q: as the output of self.tanh_layer_W will be used. right?

        self.tanh_layer_W = self.model.add_parameters((word_lstm_dim, self.sentence_level_bilstm_contexts_length))
        self.tanh_layer_b = self.model.add_parameters((word_lstm_dim))

        if self.parameters['integration_mode'] in [0, 1]:
            self.last_layer_W = self.model.add_parameters((n_tags, word_lstm_dim))
        elif self.parameters['integration_mode'] == 2:
            self.last_layer_W = self.model.add_parameters((n_tags, word_lstm_dim + 2 * mt_d))

        self.last_layer_b = self.model.add_parameters((n_tags))

        self.transform_context_layer_b = \
            self.model.add_parameters((2 * mt_d))
        self.transform_context_layer_W = \
            self.model.add_parameters((2 * mt_d, self.sentence_level_bilstm_contexts_length))

        # LSTM for words
        # self.sentence_level_bilstm_layer = \
        #     create_bilstm_layer("sentence_level",
        #                         word_representation_dim,
        #                         2 * word_lstm_dim,
        #                         bilstm=True if w_b else False)

        from toolkit.rnn import BiLSTMMultiLayeredWithShortcutConnections

        if self.parameters['multilayer']:
            self.num_sentence_level_bilstm_layers = 3
        else:
            self.num_sentence_level_bilstm_layers = 1

        self.sentence_level_bilstm_layer = \
            BiLSTMMultiLayeredWithShortcutConnections(self.num_sentence_level_bilstm_layers,
                                                      word_representation_dim,
                                                      2 * word_lstm_dim,
                                                      self.model,
                                                      CoupledLSTMBuilder,
                                                      self.parameters['shortcut_connections'])

        def _create_tying_method(activation_function=dynet.tanh, classic=True):

            def f(x, y):
                if classic:
                    return dynet.tanh(x + y)
                else:
                    return activation_function(self.tying_method_W * dynet.concatenate([x, y]) + self.tying_method_b)

            return f

        if self.parameters['tying_method']:
            self.tying_method_W = self.model.add_parameters((word_lstm_dim, 2*mt_d))
            self.tying_method_b = self.model.add_parameters((word_lstm_dim))

            self.f_tying_method = _create_tying_method(activation_function=dynet.tanh, classic=False)
        else:
            self.f_tying_method = _create_tying_method(activation_function=dynet.tanh, classic=True)

        self.crf_module = CRF(self.model, self.id_to_tag)



        # Training
        def process_hyperparameter_definition(x):
            tokens = x.split("@")
            subtokens = tokens[0].split("_")
            if len(subtokens) > 1 and subtokens[-1] == "float":
                return ["_".join(subtokens[:-1]), float(tokens[1])]
            else:
                return tokens
        _tokens = lr_method.split("-")
        opt_update_algorithm = _tokens[0]
        opt_hyperparameters = [process_hyperparameter_definition(x) for x in _tokens[1:]]
        opt_update_algorithms = {'sgd': dynet.SimpleSGDTrainer,
                                 'adam': dynet.AdamTrainer,
                                 'adadelta': dynet.AdadeltaTrainer,
                                 'adagrad': dynet.AdagradTrainer,
                                 'momentum': dynet.MomentumSGDTrainer,
                                 'rmsprop': dynet.RMSPropTrainer}

        if opt_update_algorithm == "adam":
            opt_hyperparameters += [("sparse_updates_enabled", self.parameters['sparse_updates_enabled'])]

        self.trainer = opt_update_algorithms[opt_update_algorithm](self.model,
                                                                   # sparse_updates_enabled=self.parameters['sparse_updates_enabled'],
                                                                   **{name: value for name, value in opt_hyperparameters})

        # self.trainer = dynet.SimpleSGDTrainer(self.model, learning_rate=0.01)

        self.saver = DynetSaver(self.model, self.model_path)

        return self

    def get_char_representations(self, sentence):
        # initial_state = self.char_lstm_layer.initial_state()

        char_embeddings = [[self.char_embeddings[char_id] for char_id in word]
                           for sentence_pos, word in enumerate(sentence['char_for_ids'])]

        char_representations = []
        for sentence_pos, char_embeddings_for_word in enumerate(char_embeddings):
            # print char_embeddings_for_word
            try:
                # char_representations.append(
                #     self.char_lstm_layer.transduce(char_embeddings_for_word)[-1])
                char_representations.append(self.char_lstm_layer.get_representation_concat(char_embeddings_for_word)[0])
            except IndexError as e:
                print(sentence)
                print(char_embeddings_for_word)
                print(e)
        return char_representations

    def get_sentence_level_bilstm_outputs(self,
                                          combined_word_representations,
                                          which_layer_to_use_for_morpho_disamb):
        """
        This function produces the context representations at each level given the word representations
        for each word and returns the last layer's output and the specific layer output which we want
        to use for morphological disambiguation.
         :param combined_word_representations: 
         :param which_layer_to_use_for_morpho_disamb: xyz 
         :type which_layer_to_use_for_morpho_disamb: int
         :return: two outputs: 1) layer output to be used for NER loss, 2) layer output to be used for MD loss
        """

        last_layer_context_representations, multilayered_context_representations = \
            self.sentence_level_bilstm_layer.transduce(combined_word_representations)

        last_layer_context_representations = [dynet.tanh(dynet.affine_transform([self.tanh_layer_b.expr(),
                                                                      self.tanh_layer_W.expr(),
                                                                      context])) \
                                   for context in last_layer_context_representations]
        return last_layer_context_representations, \
               multilayered_context_representations[which_layer_to_use_for_morpho_disamb-1]

    def predict(self, sentence):

        context_representations_for_ner_loss, context_representations_for_md_loss = \
            self.get_context_representations(sentence, training=False)

        last_layer_context_representations, _, _ = \
            self.get_last_layer_context_representations(sentence,
                                                        context_representations_for_ner_loss,
                                                        context_representations_for_md_loss)

        if self.parameters['active_models'] in [0, 2, 3]:
            tag_scores = self.calculate_tag_scores(last_layer_context_representations)
            # _, decoded_tags = self.crf_module.viterbi_loss(tag_scores,
            #                                                   sentence['tag_ids'])
            observations = [dynet.concatenate([obs, dynet.inputVector([-1e10, -1e10])], d=0) for obs in
                            tag_scores]
            # print("\n".join([" ".join([str(xx) for xx in x.value()]) for x in observations]))
            decoded_tags, _ = self.crf_module.viterbi_decoding(observations)
        else:
            decoded_tags = []

        # if self.parameters['integration_mode'] in [1, 2] or self.parameters['active_models'] == 1:
        if self.parameters['active_models'] in [1, 2, 3]:
            morph_analysis_representations, morph_analysis_scores = \
                self.get_morph_analysis_representations_and_scores(sentence,
                                                                   context_representations_for_md_loss)

            selected_morph_analysis_representations = \
                self.disambiguate_morph_analyzes(morph_analysis_scores)
        else:
            selected_morph_analysis_representations = []

        return selected_morph_analysis_representations, decoded_tags

    def get_loss(self, sentences_in_the_batch, loss_configuration_parameters=None):
        # immediate_compute=True, check_validity=True
        # read configuration

        dynet.renew_cg()
        loss_array = []
        for sentence in sentences_in_the_batch:
            """
            data.append({
                'str_words': str_words,
                'word_ids': words,
                'char_for_ids': chars,
                'char_lengths': [len(char) for char in chars],
                'cap_ids': caps,
                'tag_ids': tags,
                'morpho_tag_ids': morpho_tags,
                'morpho_analyzes_tags': morph_analyzes_tags,
                'morpho_analyzes_roots': morph_analyzes_roots,
                'golden_morph_analysis_indices': golden_analysis_indices,
                'sentence_lengths': len(s),
                'max_word_length_in_this_sample': max([len(x) for x in chars])
            })
            """

            """
                Our new approach will pick the appropriate model design according to the available labels.
                For example, if a sample contains only golden NER tags, only crf_loss will be added to the list of loss
                expressions to be updated. It is similar for other cases where only golden MD tags are available or when
                both are available. The latter one is only possible for Turkish.
            """

            losses_for_sentence, _ = self._get_loss(sentence)
            loss_array += losses_for_sentence

        return dynet.esum(loss_array)

    def _get_loss(self, sentence):
        loss_array = []
        tag_scores = []
        context_representations_for_ner_loss, context_representations_for_md_loss = \
            self.get_context_representations(sentence)
        last_layer_context_representations, md_loss, _ = \
            self.get_last_layer_context_representations(sentence,
                                                        context_representations_for_ner_loss,
                                                        context_representations_for_md_loss)
        if self.parameters['active_models'] in [0, 2, 3]:  # 0: NER, 1: MD, 2: JOINT, 3: JOINT_MULTILAYER
            tag_scores = self.calculate_tag_scores(last_layer_context_representations)

            if len(sentence['tag_ids']) > 0:
                crf_loss = self.crf_module.neg_log_loss(tag_scores, sentence['tag_ids'])
            else:
                crf_loss = dynet.scalarInput(0)

            if crf_loss.value() > 1000:
                logging.error("BEEP")
            loss_array.append(crf_loss)

        if self.parameters['active_models'] in [1, 2, 3]:
            loss_array.append(md_loss)

        return loss_array, tag_scores

    def _predict_for_xnlp(self, sentence):

        context_representations_for_ner_loss, context_representations_for_md_loss = \
            self.get_context_representations(sentence)
        last_layer_context_representations, _, _ = \
            self.get_last_layer_context_representations(sentence,
                                                        context_representations_for_ner_loss,
                                                        context_representations_for_md_loss)
        if self.parameters['active_models'] in [0, 2, 3]:  # 0: NER, 1: MD, 2: JOINT, 3: JOINT_MULTILAYER
            tag_scores = self.calculate_tag_scores(last_layer_context_representations)

            return tag_scores

        return None

    def _valid_path_probs(self, tag_scores, valid_paths_as_ids):

        valid_path_scores = []
        for valid_path_as_ids in valid_paths_as_ids:
            node_scores = []
            for time_t, tag_id in enumerate(valid_path_as_ids):
                node_scores.append(tag_scores[time_t][tag_id])
            if len(node_scores) == 1:
                path_score = node_scores[0]
            else:
                node_scores = dynet.concatenate(node_scores)
                path_score = log_sum_exp(node_scores)
            path_score = dynet.exp(path_score)
            valid_path_scores.append(path_score)
        sum_valid_path_scores = dynet.esum(valid_path_scores)
        valid_path_probs = dynet.cdiv(dynet.concatenate(valid_path_scores), sum_valid_path_scores)
        # valid_path_probs = [s.value()/float(dynet.esum(valid_path_scores).value()) for s in valid_path_scores]
        return valid_path_probs

    def obtain_valid_paths(self, sequence_length):
        if sequence_length in self._valid_path_cache:
            return self._valid_path_cache[sequence_length]
        else:
            self._valid_path_cache[sequence_length] = list(self._obtain_valid_paths(sequence_length))
            return self._valid_path_cache[sequence_length]

    def _obtain_valid_paths(self, sequence_length):

        # if sequence_length == 0:
        #     # yield []
        #     pass  # do not yield
        # elif sequence_length == 1:
        #     for entity_type in self.entity_types:
        #         yield ["S-%s" % entity_type]
        # else:
        #     for entity_type in self.entity_types:
        #         for right_valid_path in self._obtain_valid_paths(sequence_length - 1):
        #             yield ["S-%s" % entity_type] + right_valid_path
        #     for l in range(2, sequence_length + 1):
        #         valid_path = [""] * l
        #         valid_path[0] = "B-%s"
        #         for i in range(1, l):
        #             valid_path[i] = "I-%s"
        #         valid_path[-1] = "E-%s"
        #         for entity_type in self.entity_types:
        #             for right_valid_path in self._obtain_valid_paths(sequence_length - l):
        #                 # yield ["tag1"] + right_valid_path
        #                 yield [(x % entity_type) for x in valid_path] + right_valid_path
        #             if l == sequence_length:
        #                 # yield ["tag2"] + [l, sequence_length]
        #                 yield [(x % entity_type) for x in valid_path]

        ret = []
        if sequence_length == 0:
            ret = []
        elif sequence_length == 1:
            ret = [["O"]]
            for entity_type in self.entity_types:
                ret.append(["S-%s" % entity_type])
        else:
            ret = ["O" for _ in range(sequence_length)]
            for entity_type in self.entity_types:
                if entity_type != "OUTSIDE":
                    sub_ret = ["B-%s" % entity_type]
                    for _ in range(sequence_length-2):
                        sub_ret.append("I-%s" % entity_type)
                    sub_ret.append("E-%s" % entity_type)
                    ret.append(sub_ret)

        return ret

    def probs_for_a_specific_entity(self, sentence, entity_indices):

        valid_paths = self.obtain_valid_paths(entity_indices[-1]-entity_indices[0])
        tag_to_id = {tag: id for id, tag in self.id_to_tag.items()}
        valid_paths_as_ids = [[tag_to_id[t] for t in valid_path] for valid_path in valid_paths]

        dynet.renew_cg()
        # added this here because of a 'stale expression' error
        self.blank_morpho_tag_embedding = dynet.inputVector(list(np.zeros(self.parameters['mt_d'])))
        tag_scores = self._predict_for_xnlp(sentence)

        valid_path_probs = self._valid_path_probs(tag_scores[entity_indices[0]:entity_indices[-1]],
                                                  valid_paths_as_ids)

        valid_path_probs = valid_path_probs.value()
        if len(valid_path_probs) == 0:
            print(sentence)

        return [("NA", prob) for prob in valid_path_probs]

    def calculate_tag_scores(self, context_representations):

        tag_scores = [dynet.affine_transform([self.last_layer_b.expr(),
                                              self.last_layer_W.expr(),
                                              context]) \
                      for context in context_representations]
        return tag_scores

    def get_morph_analysis_representation_in_old_style(self, sentence):
        # these morpho_tag_ids are either chars or tags depending on the morpho_tag_type
        return [self.old_style_morpho_tag_lstm_layer_for_golden_morpho_analyzes\
                    .get_representation_concat([self.morpho_tag_embeddings[morpho_tag_id] for morpho_tag_id in morpho_tag_sequence])[0]
                for morpho_tag_sequence in sentence['morpho_tag_ids']]

    def get_combined_word_representations(self, sentence, training=None):
        """
        
        :param training:
        :param sentence: whole sentence with input values as ids
        :return: word representations made up according to the user preferences
        """

        if training is None:
            training = self.training

        representations_to_be_zipped = []
        word_embedding_based_representations = \
            [self.word_embeddings[word_id] for word_id in sentence['word_ids']]
        representations_to_be_zipped.append(dynet.concatenate([dynet.transpose(x) for x in word_embedding_based_representations]))
        char_representations = self.get_char_representations(sentence)
        representations_to_be_zipped.append(dynet.concatenate([dynet.transpose(x) for x in char_representations]))
        if self.parameters['use_golden_morpho_analysis_in_word_representation']:
            morph_tag_based_representations = self.get_morph_analysis_representation_in_old_style(sentence)
            representations_to_be_zipped.append(dynet.concatenate([dynet.transpose(x) for x in morph_tag_based_representations]))
        if self.parameters['cap_dim'] > 0:
            cap_embedding_based_representations = \
                [self.cap_embeddings[cap_id] for cap_id in sentence['cap_ids']]
            representations_to_be_zipped.append(dynet.concatenate([dynet.transpose(x) for x in cap_embedding_based_representations]))
            # combined_word_representations = [dynet.concatenate([x, y, z, xx]) for x, y, z, xx in
            #                                  zip(*representations_to_be_zipped)]
        # else:
            # combined_word_representations = [dynet.concatenate([x, y, xx]) for x, y, xx in
            #                                  zip(*representations_to_be_zipped)]

        combined_word_representations = dynet.concatenate_cols(representations_to_be_zipped)
        # print combined_word_representations
        # print self.parameters

        if training:
            combined_word_representations = [dynet.dropout(x, p=self.parameters['dropout'])
                                             for x in combined_word_representations]
        else:
            combined_word_representations = [x for x in combined_word_representations]

        return combined_word_representations

    def get_context_representations(self, sentence, training=None):
        """
        
        :param training:
        :param sentence: whole sentence with input values as ids
        :return: context representations for every layer of RNN (Bi-LSTM in our case)
        """

        if training is None:
            training = self.training

        combined_word_representations = self.get_combined_word_representations(sentence, training=training)

        context_representations_for_ner_loss, context_representations_for_md_loss = \
            self.get_sentence_level_bilstm_outputs(combined_word_representations,
                                                   1 if self.parameters['multilayer'] else 1)
        return context_representations_for_ner_loss, context_representations_for_md_loss

    def get_morph_analysis_representations(self, sentence):

        try:
            root_representations = \
                [[self.char_lstm_layer_for_morph_analysis_roots.get_representation([self.char_embeddings[char_id]
                                                              for char_id in root_char_sequence])[0]
                for root_char_sequence in root_as_char_sequences_for_word]
                for root_as_char_sequences_for_word in sentence['morpho_analyzes_roots']]
        except IndexError as e:
            print(e)
            print(root_char_sequence)

        try:
            morpho_tag_sequence_representations = \
                [[(self.morpho_tag_lstm_layer_for_morph_analysis_tags.get_representation([self.morpho_tag_embeddings[morpho_tag_id]
                                                              for morpho_tag_id in morpho_tag_sequence])[0]
                   if len(morpho_tag_sequence) > 0 else
                   (self.morpho_tag_lstm_layer_for_morph_analysis_tags.get_representation([self.blank_morpho_tag_embedding])[0]))
                for morpho_tag_sequence in morpho_tag_sequences_for_word]
                for morpho_tag_sequences_for_word in sentence['morpho_analyzes_tags']]
        except IndexError as e:
            print(e)
            print(morpho_tag_sequence)

        tyed_representations_for_every_analysis = \
            [[self.f_tying_method(root_representation, morpho_tag_representation)
             for root_representation, morpho_tag_representation in
                 zip(root_representations_for_word, morpho_tag_representations_for_word)]
             for root_representations_for_word, morpho_tag_representations_for_word in
                 zip(root_representations, morpho_tag_sequence_representations)]

        return tyed_representations_for_every_analysis