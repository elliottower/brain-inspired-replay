import numpy as np
import torch
from torch import nn
from torch.utils.data import ConcatDataset
import tqdm
import copy
import utils
from models.cl.continual_learner import ContinualLearner


def train(model, train_loader, iters, loss_cbs=list(), eval_cbs=list(), save_every=None, m_dir="./store/models",
          args=None):
    '''Train a model with a "train_a_batch" method for [iters] iterations on data from [train_loader].

    [model]             model to optimize
    [train_loader]      <dataloader> for training [model] on
    [iters]             <int> (max) number of iterations (i.e., batches) to train for
    [loss_cbs]          <list> of callback-<functions> to keep track of training progress
    [eval_cbs]          <list> of callback-<functions> to evaluate model on separate data-set'''

    device = model._device()

    # Should convolutional layers be frozen?
    freeze_convE = (utils.checkattr(args, "freeze_convE") and hasattr(args, "depth") and args.depth>0)

    # Create progress-bar (with manual control)
    bar = tqdm.tqdm(total=iters)

    iteration = epoch = 0
    while iteration < iters:
        epoch += 1

        # Loop over all batches of an epoch
        for batch_idx, (data, y) in enumerate(train_loader):
            iteration += 1

            # Perform training-step on this batch
            data, y = data.to(device), y.to(device)
            loss_dict = model.train_a_batch(data, y=y, freeze_convE=freeze_convE)

            # Fire training-callbacks (for visualization of training-progress)
            for loss_cb in loss_cbs:
                if loss_cb is not None:
                    loss_cb(bar, iteration, loss_dict, epoch=epoch)

            # Fire evaluation-callbacks (to be executed every [eval_log] iterations, as specified within the functions)
            for eval_cb in eval_cbs:
                if eval_cb is not None:
                    eval_cb(model, iteration, epoch=epoch)

            # Break if max-number of iterations is reached
            if iteration == iters:
                bar.close()
                break

            # Save checkpoint?
            if (save_every is not None) and (iteration % save_every) == 0:
                utils.save_checkpoint(model, model_dir=m_dir)



def train_cl(model, train_datasets, replay_mode="none", scenario="task", rnt=None, classes_per_task=None,
             iters=2000, batch_size=32, batch_size_replay=None, loss_cbs=list(), eval_cbs=list(), sample_cbs=list(),
             generator=None, gen_iters=0, gen_loss_cbs=list(), feedback=False, reinit=False, args=None, only_last=False,
             sample_method='random', curated_multiplier=4, variety_weight=0.5, mir_coef=0.1):
    '''Train a model (with a "train_a_batch" method) on multiple tasks, with replay-strategy specified by [replay_mode].

    [model]             <nn.Module> main model to optimize across all tasks
    [train_datasets]    <list> with for each task the training <DataSet>
    [replay_mode]       <str>, choice from "generative", "current", "offline" and "none"
    [scenario]          <str>, choice from "task", "domain", "class" and "all"
    [classes_per_task]  <int>, # classes per task; only 1st task has [classes_per_task]*[first_task_class_boost] classes
    [rnt]               <float>, indicating relative importance of new task (if None, relative to # old tasks)
    [iters]             <int>, # optimization-steps (=batches) per task; 1st task has [first_task_iter_boost] steps more
    [batch_size_replay] <int>, number of samples to replay per batch
    [generator]         None or <nn.Module>, if a seperate generative model should be trained (for [gen_iters] per task)
    [feedback]          <bool>, if True and [replay_mode]="generative", the main model is used for generating replay
    [only_last]         <bool>, only train on final task / episode
    [*_cbs]             <list> of call-back functions to evaluate training-progress
    [sample_method]     <str> indicating the sample method, choices: 'random', 'uniform', 'curated', 'softmax', 'interfered', 'misclassified'
    [curated_multiplier]<int> choose curated samples out of size curated_multiplier * mutiply batch_size_replay
    [variety_weight]    <float> weight of variety loss as compared with regular loss
    [mir_coef]          <float> weight of previous model's cross entropy score for generated sample (encourages samples which the prior model is confident about)

    '''

    print("\nCurated multiplier: ", curated_multiplier)
    print("\nVariety weight: ", variety_weight)
    print("\nMIR coefficient: ", mir_coef)
    # Should convolutional layers be frozen?
    freeze_convE = (utils.checkattr(args, "freeze_convE") and hasattr(args, "depth") and args.depth>0)

    # Use cuda?
    device = model._device()
    cuda = model._is_on_cuda()

    # Set default-values if not specified
    batch_size_replay = batch_size if batch_size_replay is None else batch_size_replay

    # Initiate indicators for replay (no replay for 1st task)
    Generative = Current = Offline_TaskIL = False
    previous_model = None

    # Register starting param-values (needed for "intelligent synapses").
    if isinstance(model, ContinualLearner) and model.si_c>0:
        for n, p in model.named_parameters():
            if p.requires_grad:
                n = n.replace('.', '__')
                model.register_buffer('{}_SI_prev_task'.format(n), p.detach().clone())

    # Loop over all tasks.
    for task, train_dataset in enumerate(train_datasets, 1):

        # If offline replay-setting, create large database of all tasks so far
        if replay_mode=="offline" and (not scenario=="task"):
            train_dataset = ConcatDataset(train_datasets[:task])
        # -but if "offline"+"task": all tasks so far should be visited separately (i.e., separate data-loader per task)
        if replay_mode=="offline" and scenario=="task":
            Offline_TaskIL = True
            data_loader = [None]*task

        # Initialize # iters left on data-loader(s)
        iters_left = 1 if (not Offline_TaskIL) else [1]*task

        # Prepare <dicts> to store running importance estimates and parameter-values before update
        if isinstance(model, ContinualLearner) and model.si_c>0:
            W = {}
            p_old = {}
            for n, p in model.named_parameters():
                if p.requires_grad:
                    n = n.replace('.', '__')
                    W[n] = p.data.clone().zero_()
                    p_old[n] = p.data.clone()

        # Find [active_classes] (=classes in current task)
        active_classes = None  #-> for "domain"- or "all"-scenarios, always all classes are active
        if scenario=="task":
            # -for "task"-scenario, create <list> with for all tasks so far a <list> with the active classes
            active_classes = [list(range(classes_per_task*i, classes_per_task*(i+1))) for i in range(task)]
        elif scenario=="class":
            # -for "class"-scenario, create one <list> with active classes of all tasks so far
            active_classes = list(range(classes_per_task*task))

        # Reinitialize the model's parameters (if requested)
        if reinit:
            from define_models import init_params
            init_params(model, args)
            if generator is not None:
                init_params(generator, args)

        # Define a tqdm progress bar(s)
        iters_main = iters
        progress = tqdm.tqdm(range(1, iters_main+1))
        if generator is not None:
            iters_gen = gen_iters
            progress_gen = tqdm.tqdm(range(1, iters_gen+1))

        # Loop over all iterations
        iters_to_use = (iters_main if (generator is None) else max(iters_main, iters_gen))
        # -if only the final task should be trained on:
        if only_last and not task==len(train_datasets):
            iters_to_use = 0

        # This helps w/ speeding up curated_classVariety
        mask = None
        if (sample_method=="curated_classVariety" and (task-1)>0):
            sampleAmt = batch_size_replay * curated_multiplier
            classNum = classes_per_task*(task-1)
            indexList = [[idx for idx in range(sampleAmt) if (idx%classNum) == (rowIdx%classNum)] for rowIdx in range(sampleAmt)]
            mask = []
            for rowIdxList in indexList:
                curRow = [0] * sampleAmt
                for idx in rowIdxList:
                    curRow[idx] = 1
                mask.append(curRow)
            mask = torch.tensor(mask, dtype=torch.float).to(device)


        for batch_index in range(1, iters_to_use+1):

            # Update # iters left on current data-loader(s) and, if needed, create new one(s)
            if not Offline_TaskIL:
                iters_left -= 1
                if iters_left==0:
                    data_loader = iter(utils.get_data_loader(train_dataset, batch_size, cuda=cuda, drop_last=True))
                    iters_left = len(data_loader)
            else:
                # -with "offline replay" in Task-IL scenario, there is a separate data-loader for each task
                batch_size_to_use = int(np.ceil(batch_size/task))
                for task_id in range(task):
                    iters_left[task_id] -= 1
                    if iters_left[task_id]==0:
                        data_loader[task_id] = iter(utils.get_data_loader(
                            train_datasets[task_id], batch_size_to_use, cuda=cuda, drop_last=True
                        ))
                        iters_left[task_id] = len(data_loader[task_id])



            #-----------------Collect data------------------#

            #####-----CURRENT BATCH-----#####
            if not Offline_TaskIL:
                x, y = next(data_loader)                                    #--> sample training data of current task
                y = y-classes_per_task*(task-1) if scenario=="task" else y  #--> ITL: adjust y-targets to 'active range'
                x, y = x.to(device), y.to(device)                           #--> transfer them to correct device
                #y = y.expand(1) if len(y.size())==1 else y                 #--> hack for if batch-size is 1
            else:
                x = y = task_used = None  #--> all tasks are "treated as replay"
                # -sample training data for all tasks so far, move to correct device and store in lists
                x_, y_ = list(), list()
                for task_id in range(task):
                    x_temp, y_temp = next(data_loader[task_id])
                    x_.append(x_temp.to(device))
                    y_temp = y_temp - (classes_per_task * task_id) #--> adjust y-targets to 'active range'
                    if batch_size_to_use == 1:
                        y_temp = torch.tensor([y_temp])            #--> correct dimensions if batch-size is 1
                    y_.append(y_temp.to(device))


            #####-----REPLAYED BATCH-----#####
            if not Offline_TaskIL and not Generative and not Current:
                x_ = y_ = scores_ = task_used = None   #-> if no replay

            #--------------------------------------------INPUTS----------------------------------------------------#

            ##-->> Current Replay <<--##
            if Current:
                x_ = x[:batch_size_replay]  #--> use current task inputs
                task_used = None


            ##-->> Generative Replay <<--##
            if Generative:
                #---> Only with generative replay, the resulting [x_] will be at the "hidden"-level
                conditional_gen = True if (
                    (previous_generator.per_class and previous_generator.prior=="GMM") or
                    utils.checkattr(previous_generator, 'dg_gates')
                ) else False

                # Sample [x_]
                if conditional_gen and scenario=="task":
                    # -if a conditional generator is used with task-IL scenario, generate data per previous task
                    x_ = list()
                    task_used = list()
                    for task_id in range(task-1):
                        allowed_classes = list(range(classes_per_task*task_id, classes_per_task*(task_id+1)))
                        batch_size_replay_to_use = int(np.ceil(batch_size_replay / (task-1)))
                        x_temp_ = previous_generator.sample(batch_size_replay_to_use, allowed_classes=allowed_classes,
                                                            only_x=False)
                        x_.append(x_temp_[0])
                        task_used.append(x_temp_[2])
                else:
                    # -which classes are allowed to be generated? (relevant if conditional generator / decoder-gates)
                    allowed_classes = None if scenario=="domain" else list(range(classes_per_task*(task-1)))
                    # -which tasks/domains are allowed to be generated? (only relevant if "Domain-IL" with task-gates)
                    allowed_domains = list(range(task-1))
                    # -generate inputs representative of previous tasks

                    # --- SAMPLE METHOD CHOICES: softmax, random, uniform, curated ---
                    # --- Softmax sampling: use previous model to score images from this new task, generate those classes
                    if sample_method == 'softmax':
                        with torch.no_grad():
                            curTaskID = task - 2
                            newScores_og = previous_model.classify(previous_model.input_to_hidden(x),
                                                                   not_hidden=False if Generative else True)
                            newScores = newScores_og[:, :(classes_per_task * (curTaskID + 1))]
                            softmax = torch.nn.Softmax(dim=1)
                            scores_old = nn.Softmax(dim=1)(newScores)
                            avgError = torch.mean(scores_old, dim=0)
                            sampleProbs = torch.zeros(newScores_og.shape[1])
                            sampleProbs[:(classes_per_task * (curTaskID + 1))] = avgError[
                                                                                 :(classes_per_task * (curTaskID + 1))]
                            x_, y_used, task_used = previous_generator.sample(
                                batch_size_replay, allowed_classes=allowed_classes, allowed_domains=allowed_domains,
                                only_x=False, class_probs=sampleProbs,uniform_sampling=False)
                        
                    # --- Uniformly random sampling (baseline) ---
                    elif sample_method == 'random':
                        x_, y_used, task_used = previous_generator.sample(
                            batch_size_replay, allowed_classes=allowed_classes, allowed_domains=allowed_domains,
                            only_x=False, class_probs=None, uniform_sampling=False)

                    # --- Uniform sampling: balanced numbers of samples from each class ---
                    elif sample_method == 'uniform':
                        x_, y_used, task_used = previous_generator.sample(
                            batch_size_replay, allowed_classes=allowed_classes, allowed_domains=allowed_domains,
                            only_x=False, class_probs=None, uniform_sampling=True)
                    # --- Uniform sample curation: pick the best samples to show (by some metric), balance uniformly ---
                    else:

                        if (sample_method == "curated_variety" or sample_method == "interfered"):
                            # Generate x times as many samples as we need to then pick the best of
                            x_, y_used, task_used, varietyVector = previous_generator.sample(
                                batch_size_replay * curated_multiplier, allowed_classes=allowed_classes, allowed_domains=allowed_domains,
                                only_x=False, class_probs=None, uniform_sampling=False, varietyVector=True)

                        # CURATED USING CLASS VARIETY (i.e., generating batch_size_reply*curated_multipler / len(allowed_classes) samples 
                        # per class, where each sample is the "most different" sample based off our variety calculation 
                        elif(sample_method == "curated_classVariety"):
                            x_, y_used, task_used, varietyVector = previous_generator.sample(
                                batch_size_replay * curated_multiplier, allowed_classes=allowed_classes, allowed_domains=allowed_domains,
                                only_x=False, class_probs=None, uniform_sampling=True, varietyVector=True, classVariety=True, classVarietyMask=mask)

                        elif(sample_method == "curated_softmax"):

                            with torch.no_grad():
                                curTaskID = task - 2
                                newScores_og = previous_model.classify(previous_model.input_to_hidden(x),
                                                                       not_hidden=False if Generative else True)
                                newScores = newScores_og[:, :(classes_per_task * (curTaskID + 1))]
                                softmax = torch.nn.Softmax(dim=1)
                                scores_old = nn.Softmax(dim=1)(newScores)
                                avgError = torch.mean(scores_old, dim=0)
                                sampleProbs = torch.zeros(newScores_og.shape[1])
                                sampleProbs[:(classes_per_task * (curTaskID + 1))] = avgError[
                                                                                     :(classes_per_task * (curTaskID + 1))]

                            # Generate x times as many samples as we need to then pick the best of
                            x_, y_used, task_used = previous_generator.sample(
                                batch_size_replay * curated_multiplier, allowed_classes=allowed_classes, allowed_domains=allowed_domains,
                                only_x=False, class_probs=sampleProbs, uniform_sampling=False)


                        else: 
                            # Generate x times as many samples as we need to then pick the best of
                            x_, y_used, task_used = previous_generator.sample(
                                batch_size_replay * curated_multiplier, allowed_classes=allowed_classes, allowed_domains=allowed_domains,
                                only_x=False, class_probs=None, uniform_sampling=False)

                        # --- Measure the performance of each of these samples on the current model ---
                        # Use the previous model to score the generated images (code taken from Trevor's softmax above)
                        with torch.no_grad():
                            curTaskID = task - 2
                            newScores_og = model.classify(x_, not_hidden=False if Generative else True).to(device)
                            newScores = newScores_og[:, :(classes_per_task * (curTaskID + 1))].to(device) # Logits that don't sum to 1
                            scores_old = nn.Softmax(dim=1)(newScores).to(device) # Makes the scores sum to 1 (probabilities)
                            cross_entropy = nn.CrossEntropyLoss(reduction='none').to(device)
                            y_used = torch.tensor(y_used, dtype=torch.long).to(device)
                            loss_old = cross_entropy(scores_old, y_used).to(device)

                        # --- Copy the model and perform an update on just the new incoming data (no replayed data) ---
                        # This will lead to catastrophic forgetting, as it has no replays to prevent this from happening
                        model_tmp = copy.deepcopy(model)
                        # NOTE: Can train multiple batches if needed, but it would be on the same data, so any changes will just be exacerbated
                        _ = model_tmp.train_a_batch(x, y=y, x_=None, y_=None, scores_=None,
                                                        tasks_=task_used, active_classes=active_classes, task=task, rnt=(
                                                            1. if task==1 else 1./task
                                                        ) if rnt is None else rnt, freeze_convE=freeze_convE,
                                                        replay_not_hidden=False if Generative else True)

                        # --- Measure the performance of each of the generated samples on this updated model ---
                        # This can tell us how much the model 'forgets' each of these samples, we will replay the worst ones
                        with torch.no_grad():
                            curTaskID = task - 2
                            newScores_og = model_tmp.classify(x_, not_hidden=False if Generative else True).to(device)
                            newScores = newScores_og[:, :(classes_per_task * (curTaskID + 2))].to(device) # Logits that don't sum to 1
                            scores_new = nn.Softmax(dim=1)(newScores).to(device) # Makes the scores sum to 1 (probabilities)

                            # --- Measure the difference in cross entropy loss for predictions before and after ---
                            if sample_method == 'curated' or sample_method == "curated_softmax":
                                cross_entropy = nn.CrossEntropyLoss(reduction='none') # Per-example cross entropy (not avg)
                                loss_new = cross_entropy(scores_new, y_used)

                                # Amount that the loss changes between the model updating
                                diff = loss_new - loss_old
                                metric = diff

                            # TREVOR'S NEW METHOD - This tries to take into account the variety of the samples
                            elif sample_method == "curated_variety" or sample_method == "curated_classVariety":
                                cross_entropy = nn.CrossEntropyLoss(reduction='none').to(device) # Per-example cross entropy (not avg)
                                loss_new = cross_entropy(scores_new, y_used).to(device)

                                # Amount that the loss changes between the model updating
                                diff = loss_new - loss_old
                                
                                # Softmaxing diff and the variety vector (to get probabilities)
                                variety_weight = torch.tensor((variety_weight)).to(device)
                                diff_softmax = nn.Softmax(dim=0)(diff).to(device)
                                variety_softmax = nn.Softmax(dim=0)(varietyVector).to(device)
                                metric = ((1-variety_weight) * diff_softmax) + (variety_weight * variety_softmax).to(device)

                            # Multiply the misclassification error (cross entropy) by the amount that this changes between the model updating
                            # metric = loss_new * diff

                            # --- Measure KL Divergence between predictions before and predictions afterwards ---
                            # Maximally Interfered Retrieval uses a linear combination of KL, entropy, and 'variance'
                            # This ensures the samples are not too close together, but we do not currently measure that
                            elif sample_method == 'interfered':
                                # First, pad with zeros so predictions match (previous model predicts zero score for all new classes)
                                padded_scores = torch.zeros_like(scores_new)
                                padded_scores[:, :scores_old.size(1)] = scores_old
                                kl_div = nn.KLDivLoss(reduction='none')(padded_scores, scores_new)
                                kl_div = torch.mean(kl_div, dim=1)
                                variety_softmax = nn.Softmax(dim=0)(varietyVector).to(device)

                                # Calculate MIR loss and balance with variety (instead of explicitly searching, maximize both variety and MIR loss)
                                mir_loss = kl_div - mir_coef * loss_old
                                metric = (1-variety_weight) * (mir_loss) + variety_weight * variety_softmax

                            # --- New idea: use the examples which the new model misclassifies the most as one of the new classes
                            # This the opposite approach to softmax, where softmax takes the current model and calculates
                            # Which classes does it confuse the new data for the most, this trains on the new data and then
                            # Tries to find generated examples which it confuses for the new data classes the most
                            elif sample_method == 'misclassified' or sample_method == 'uniform_large' or sample_method == 'random_large':
                                metric = scores_new[:, -1] + scores_new[:, -1]

                            # --- Sort based on some metric, then divide up by classes (afterwards) ---
                            _, indices = torch.sort(metric, descending=True) # Descending order, pick first 100

                            # Shuffle indices around to test choosing from this larger pool of generated samples randomly
                            if sample_method == 'uniform_large' or sample_method == 'random_large':
                                indices2 = indices.cpu().numpy()
                                np.random.shuffle(indices2)
                                indices = torch.from_numpy(indices2).to(device)

                            if sample_method != 'random_large' and sample_method != 'curated_softmax':
                                # --- Calculate how many examples for each class should be generated to divide up uniformly ---
                                # Uniform dist will be [0, 1, 2, 3, 0, 1, 2] for allowed classes=4 and batch_size_replay=7
                                uniform_dist = torch.arange(batch_size_replay) % len(allowed_classes)
                                counts_each_class = torch.unique(uniform_dist, return_counts=True)[1]

                                # --- Optional: Calculate unbalanced indices to replay, results in poor performance ---
                                # If we added a variation term to ensure samples are different from each other, this could
                                # be a simpler way to do things, but variance would be pretty complicated to calculate
                                #indices_to_replay = indices[:batch_size_replay]

                                # --- Select the top k_i indices for each class i, where k_i is the number of examples for that class ---
                                # Top x most affected of the generated samples for each class (ensures it is balanced, slightly more computation than unbalanced)
                                indices_to_replay = torch.cat(( [ indices[y_used[indices]==i][:counts_each_class[i]] for i in range(len(allowed_classes)) ] ))
                                x_ = x_[indices_to_replay]
                            else:
                                # Uniformly randomly choose from the 400 samples generated
                                x_ = x_[indices]

            #--------------------------------------------OUTPUTS----------------------------------------------------#

            if Generative or Current:
                # Get target scores & possibly labels (i.e., [scores_] / [y_]) -- use previous model, with no_grad()
                if scenario in ("domain", "class") and previous_model.mask_dict is None:
                    # -if replay does not need to be evaluated for each task (ie, not Task-IL and no task-specific mask)
                    with torch.no_grad():
                        all_scores_ = previous_model.classify(x_, not_hidden=False if Generative else True)
                    scores_ = all_scores_[:, :(classes_per_task*(task-1))] if (
                            scenario=="class"
                    ) else all_scores_ # -> when scenario=="class", zero probs will be added in [loss_fn_kd]-function
                    # -also get the 'hard target'
                    _, y_ = torch.max(scores_, dim=1)
                else:
                    # -[x_] needs to be evaluated according to each previous task, so make list with entry per task
                    scores_ = list()
                    y_ = list()
                    # -if no task-mask and no conditional generator, all scores can be calculated in one go
                    if previous_model.mask_dict is None and not type(x_)==list:
                        with torch.no_grad():
                            all_scores_ = previous_model.classify(x_, not_hidden=False if Generative else True)
                    for task_id in range(task-1):
                        # -if there is a task-mask (i.e., XdG is used), obtain predicted scores for each task separately
                        if previous_model.mask_dict is not None:
                            previous_model.apply_XdGmask(task=task_id+1)
                        if previous_model.mask_dict is not None or type(x_)==list:
                            with torch.no_grad():
                                all_scores_ = previous_model.classify(x_[task_id] if type(x_)==list else x_,
                                                                      not_hidden=False if Generative else True)
                        if scenario=="domain":
                            # NOTE: if scenario=domain with task-mask, it's of course actually the Task-IL scenario!
                            #       this can be used as trick to run the Task-IL scenario with singlehead output layer
                            temp_scores_ = all_scores_
                        else:
                            temp_scores_ = all_scores_[:, (classes_per_task*task_id):(classes_per_task*(task_id+1))]
                        scores_.append(temp_scores_)
                        # - also get hard target
                        _, temp_y_ = torch.max(temp_scores_, dim=1)
                        y_.append(temp_y_)
            # -only keep predicted y_/scores_ if required (as otherwise unnecessary computations will be done)
            y_ = y_ if (model.replay_targets=="hard") else None
            scores_ = scores_ if (model.replay_targets=="soft") else None



            #-----------------Train model(s)------------------#

            #---> Train MAIN MODEL
            if batch_index <= iters_main:

                # Train the main model with this batch
                loss_dict = model.train_a_batch(x, y=y, x_=x_, y_=y_, scores_=scores_,
                                                tasks_=task_used, active_classes=active_classes, task=task, rnt=(
                                                    1. if task==1 else 1./task
                                                ) if rnt is None else rnt, freeze_convE=freeze_convE,
                                                replay_not_hidden=False if Generative else True)


                # UNIFORM SAMPLE CURATION: loss_dict has a "predL_r" key that contains the individual prediction
                # losses 



                # Update running parameter importance estimates in W
                if isinstance(model, ContinualLearner) and model.si_c>0:
                    for n, p in model.convE.named_parameters():
                        if p.requires_grad:
                            n = "convE."+n
                            n = n.replace('.', '__')
                            if p.grad is not None:
                                W[n].add_(-p.grad*(p.detach()-p_old[n]))
                            p_old[n] = p.detach().clone()
                    for n, p in model.fcE.named_parameters():
                        if p.requires_grad:
                            n = "fcE."+n
                            n = n.replace('.', '__')
                            if p.grad is not None:
                                W[n].add_(-p.grad * (p.detach() - p_old[n]))
                            p_old[n] = p.detach().clone()
                    for n, p in model.classifier.named_parameters():
                        if p.requires_grad:
                            n = "classifier."+n
                            n = n.replace('.', '__')
                            if p.grad is not None:
                                W[n].add_(-p.grad * (p.detach() - p_old[n]))
                            p_old[n] = p.detach().clone()

                # Fire callbacks (for visualization of training-progress / evaluating performance after each task)
                for loss_cb in loss_cbs:
                    if loss_cb is not None:
                        loss_cb(progress, batch_index, loss_dict, task=task)
                for eval_cb in eval_cbs:
                    if eval_cb is not None:
                        eval_cb(model, batch_index, task=task)
                if model.label=="VAE":
                    for sample_cb in sample_cbs:
                        if sample_cb is not None:
                            sample_cb(model, batch_index, task=task, allowed_classes=None if (
                                    scenario=="domain"
                            ) else list(range(classes_per_task*task)))


            #---> Train GENERATOR
            if generator is not None and batch_index <= iters_gen:

                loss_dict = generator.train_a_batch(x, y=y, x_=x_, y_=y_, scores_=scores_,
                                                    tasks_=task_used, active_classes=active_classes, rnt=(
                                                        1. if task==1 else 1./task
                                                    ) if rnt is None else rnt, task=task,
                                                    freeze_convE=freeze_convE,
                                                    replay_not_hidden=False if Generative else True)

                # Fire callbacks on each iteration
                for loss_cb in gen_loss_cbs:
                    if loss_cb is not None:
                        loss_cb(progress_gen, batch_index, loss_dict, task=task)
                for sample_cb in sample_cbs:
                    if sample_cb is not None:
                        sample_cb(generator, batch_index, task=task, allowed_classes=None if (
                                    scenario=="domain"
                            ) else list(range(classes_per_task*task)))


        # Close progres-bar(s)
        progress.close()
        if generator is not None:
            progress_gen.close()


        ##----------> UPON FINISHING EACH TASK...

        # EWC: estimate Fisher Information matrix (FIM) and update term for quadratic penalty
        if isinstance(model, ContinualLearner) and model.ewc_lambda>0:
            # -find allowed classes
            allowed_classes = list(
                range(classes_per_task*(task-1), classes_per_task*task)
            ) if scenario=="task" else (list(range(classes_per_task*task)) if scenario=="class" else None)
            # -if needed, apply correct task-specific mask
            if model.mask_dict is not None:
                model.apply_XdGmask(task=task)
            # -estimate FI-matrix
            model.estimate_fisher(train_dataset, allowed_classes=allowed_classes)

        # SI: calculate and update the normalized path integral
        if isinstance(model, ContinualLearner) and model.si_c>0:
            model.update_omega(W, model.epsilon)

        # REPLAY: update source for replay
        previous_model = copy.deepcopy(model).eval()
        if replay_mode=="generative":
            Generative = True
            previous_generator = previous_model if feedback else copy.deepcopy(generator).eval()
        elif replay_mode=='current':
            Current = True
